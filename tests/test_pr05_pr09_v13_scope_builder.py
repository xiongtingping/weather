from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.model_family_registry import (
    REGISTRY_ROWS,
    canonical_independence_family_id,
)
from bayes_basket_target.model_product_identity import (
    CANONICAL_INDEPENDENCE_BY_PRODUCT,
    COVERAGE_PRODUCT_IDS,
    MODEL_FAMILY_BY_PRODUCT,
    NATIVE_PRODUCT_IDS,
    PRODUCT_ROWS,
    build_model_product_identity_receipt,
    build_registered_model_product_identity_receipt,
)
from bayes_basket_target.model_system_coverage import (
    LEADS,
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
)
from bayes_basket_target.pr05_pr09_v13_scope_builder import (
    UNPROVEN_V13_EVENT_FIELDS,
    build_pr05_pr09_v13_scope_bundle,
)


AS_OF = "2026-08-03T21:08:00Z"
ROOT = Path(__file__).resolve().parents[1]
COVERAGE_V5 = ROOT / "output/audits/pr05_pr09_model_coverage_v5_20260804.json"


def _matrix(*, product_id: str = "ECMWF") -> dict:
    rows = []
    for station_id in canonical_station_ids():
        for lead_bucket in LEADS:
            rows.append(make_coverage_row(
                station_id=station_id,
                product_id=product_id,
                provider_id="ecmwf_ifs_open_meteo_previous_runs",
                model_family="global_deterministic",
                independence_family_id="DETERMINISTIC_ECMWF",
                source_class="aggregator_republisher",
                lead_bucket=lead_bucket,
                run_cycle=None if lead_bucket == "d0" else (
                    "PREVIOUS_DAY1" if lead_bucket == "d1" else "PREVIOUS_DAY2"
                ),
                as_of_utc=AS_OF,
                state="BLOCKED",
                blockers=["DATA_MISSING" if lead_bucket != "d0" else "RUN_CYCLE_MISMATCH"],
                evidence_role="historical_strict_oos",
                strict_asof=False,
                closed_awc_truth=False,
                train_count=0,
                holdout_count=0,
                source_evidence_sha256="1" * 64,
                raw_receipt_sha256=None,
                oos_evidence_sha256=None,
                single_system_artifact_sha256=None,
                native_arrival_indexed=False,
                formal_live_authority=False,
                cross_selection_count=0,
                cross_activation_count=0,
                p0_count=0,
            ))
    return build_coverage_matrix(rows, expected_product_ids=(product_id,), as_of_utc=AS_OF)


def test_resolves_product_identity_but_keeps_event_data_terminal() -> None:
    matrix = _matrix()
    bundle = build_pr05_pr09_v13_scope_bundle(matrix, as_of_utc=AS_OF)

    assert bundle["state"] == "BLOCKED"
    assert bundle["coverage_scope_count"] == 135
    assert bundle["v13_expected_scope_count"] == 135
    assert len(bundle["v13_expected_scopes"]) == 135
    assert bundle["identity_ready_event_data_missing_count"] == 135
    assert bundle["lineage_mismatch_count"] == 0
    assert bundle["terminal_scope_count"] == 135
    assert len(bundle["unresolved_input_receipts"]) == 135
    assert len(set(bundle["unresolved_input_receipts"])) == 135
    first = bundle["terminal_scopes"][0]
    assert first["blocker"] == "DATA_MISSING"
    assert first["expected_key_emitted"] is True
    assert first["unproven_v13_identity_fields"] == []
    assert first["unproven_v13_event_fields"] == list(UNPROVEN_V13_EVENT_FIELDS)
    assert first["model_product_identity_receipt"]["settlement_timezone"] == "America/New_York"
    assert first["coverage_scope"]["product_id"] == "ECMWF"
    assert first["coverage_scope"]["station_id"] in canonical_station_ids()


def test_rejects_different_asof_instead_of_relabeling_matrix() -> None:
    with pytest.raises(ContractError, match="as_of_mismatch"):
        build_pr05_pr09_v13_scope_bundle(
            _matrix(), as_of_utc="2026-08-03T21:08:01Z"
        )


def test_rejects_tampered_coverage_before_generating_terminals() -> None:
    matrix = copy.deepcopy(_matrix())
    matrix["rows"][0]["provider_id"] = "OTHER_PROVIDER"
    with pytest.raises(ContractError, match="content_sha"):
        build_pr05_pr09_v13_scope_bundle(matrix, as_of_utc=AS_OF)


def test_unknown_product_cannot_be_inferred_from_coverage_fields() -> None:
    with pytest.raises(ContractError, match="product_unknown"):
        build_pr05_pr09_v13_scope_bundle(
            _matrix(product_id="TEST_PRODUCT"), as_of_utc=AS_OF
        )


def test_real_coverage_v5_resolves_exact_product_matrix_and_blockers() -> None:
    matrix = json.loads(COVERAGE_V5.read_text(encoding="utf-8"))
    bundle = build_pr05_pr09_v13_scope_bundle(
        matrix, as_of_utc=matrix["as_of_utc"]
    )

    expected_products = set(COVERAGE_PRODUCT_IDS)
    assert expected_products == set(matrix["product_ids"])
    assert len(expected_products) == 21
    assert set(CANONICAL_INDEPENDENCE_BY_PRODUCT) == {
        row["product_id"] for row in PRODUCT_ROWS
    }
    assert len(PRODUCT_ROWS) == 24
    assert bundle["coverage_scope_count"] == 2835
    assert bundle["v13_expected_scope_count"] == 2520
    assert bundle["identity_ready_event_data_missing_count"] == 2520
    assert bundle["lineage_mismatch_count"] == 315
    assert bundle["terminal_scope_count"] == 2835
    assert bundle["consumable"] is False

    mismatch_scopes = {
        (
            item["coverage_scope"]["product_id"],
            item["coverage_scope"]["lead_bucket"],
        )
        for item in bundle["terminal_scopes"]
        if item["blocker"] == "LINEAGE_MISMATCH"
    }
    assert mismatch_scopes == {
        ("DWD_ICON_GLOBAL_EPS", "d0"),
        ("DWD_ICON_GLOBAL_EPS", "d1"),
        ("DWD_ICON_GLOBAL_EPS", "d2"),
        ("NOAA_GEFS_05", "d0"),
        ("NOAA_GEFS_05", "d1"),
        ("ECCC_GEM_GLOBAL_ENS", "d0"),
        ("ECCC_GEM_GLOBAL_ENS", "d1"),
    }
    for scope in bundle["v13_expected_scopes"]:
        assert canonical_independence_family_id(
            model_id=scope["model_id"],
            provider_id=scope["provider_id"],
            provider_product_id=scope["provider_product_id"],
            source_class=scope["source_class"],
        ) == scope["independence_family_id"]


def test_family_registry_has_one_exact_key_per_row() -> None:
    keys = [
        (
            row["model_id"], row["provider_id"],
            row["provider_product_id"], row["source_class"],
        )
        for row in REGISTRY_ROWS
    ]
    assert len(keys) == len(set(keys))


def test_direct_identity_adapter_rejects_coverage_provider_and_source_tamper() -> None:
    row = _matrix()["rows"][0]
    for field, value in (
        ("provider_id", "OPEN_METEO"),
        ("source_class", "native_official"),
    ):
        tampered = copy.deepcopy(row)
        tampered[field] = value
        with pytest.raises(ContractError, match="content_sha_mismatch"):
            build_model_product_identity_receipt(tampered)


def test_identity_adapter_rejects_resealed_wrong_product_model_family() -> None:
    row = copy.deepcopy(_matrix()["rows"][0])
    row["model_family"] = "regional"
    row_without_hash = {key: value for key, value in row.items() if key != "content_sha256"}
    from bayes_basket_target.contracts import content_sha256

    row["content_sha256"] = content_sha256(row_without_hash)
    with pytest.raises(ContractError, match="coverage_mapping_mismatch"):
        build_model_product_identity_receipt(row)


def test_direct_identity_adapter_rejects_noncanonical_timezone_field() -> None:
    row = copy.deepcopy(_matrix()["rows"][0])
    row["settlement_timezone"] = "Asia/Tokyo"
    with pytest.raises(ContractError, match="unknown_field:settlement_timezone"):
        build_model_product_identity_receipt(row)


def test_native_null_cycle_remains_lineage_mismatch() -> None:
    matrix = json.loads(COVERAGE_V5.read_text(encoding="utf-8"))
    native_null = next(
        row for row in matrix["rows"]
        if row["product_id"] == "NOAA_GEFS_05" and row["run_cycle"] is None
    )
    identity, missing = build_model_product_identity_receipt(native_null)
    assert identity is None
    assert missing == ("run_cycle", "run_cycle_scope")


def test_all_products_have_exact_model_family_mapping() -> None:
    assert set(MODEL_FAMILY_BY_PRODUCT) == {
        row["product_id"] for row in PRODUCT_ROWS
    }


@pytest.mark.parametrize(
    ("product_id", "model_id", "provider_id", "provider_product_id", "family"),
    (
        ("ECMWF_IFS_0P25", "ECMWF", "ecmwf_ifs025_official", "ecmwf_ifs025", "ECMWF"),
        ("NOAA_GFS_0P25", "GFS", "gfs_0p25_official", "noaa_gfs_0p25", "NOAA"),
        ("DWD_ICON_GLOBAL", "ICON", "icon_global_official", "dwd_icon_global", "DWD"),
    ),
)
def test_registered_native_deterministic_identity_is_exact(
    product_id: str, model_id: str, provider_id: str,
    provider_product_id: str, family: str,
) -> None:
    assert product_id in NATIVE_PRODUCT_IDS
    receipt = build_registered_model_product_identity_receipt(
        product_id=product_id, station_id="KATL", lead_bucket="d0",
        run_cycle="18Z", identity_source_receipt_sha256="a" * 64,
    )
    assert receipt["model_id"] == model_id
    assert receipt["provider_id"] == provider_id
    assert receipt["provider_product_id"] == provider_product_id
    assert receipt["independence_family_id"] == family
    assert receipt["source_class"] == "native_official"
    assert receipt["identity_source_kind"] == "NATIVE_DECODE_EVIDENCE"
    assert receipt["consumable"] is False

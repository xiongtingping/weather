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


def test_direct_identity_adapter_rejects_noncanonical_timezone_field() -> None:
    row = copy.deepcopy(_matrix()["rows"][0])
    row["settlement_timezone"] = "Asia/Tokyo"
    with pytest.raises(ContractError, match="unknown_field:settlement_timezone"):
        build_model_product_identity_receipt(row)


def test_all_products_have_exact_model_family_mapping() -> None:
    assert set(MODEL_FAMILY_BY_PRODUCT) == {
        row["product_id"] for row in PRODUCT_ROWS
    }


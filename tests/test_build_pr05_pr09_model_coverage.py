from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_system_coverage import (
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
    validate_coverage_matrix,
)
from scripts.build_pr05_pr09_model_coverage import (
    COMPILED_AT_UTC,
    EXPECTED_PRODUCT_IDS,
    EXPECTED_SCOPE_COUNT,
    PR05_PRODUCTS,
    PR06_PRODUCTS,
    PR07_PRODUCTS,
    PR08_PR09_PRODUCTS,
    build_pr05_pr09_coverage_matrix,
    main,
)
from bayes_basket_target.fleet import expected_station_count


def _matrix(products: tuple[str, ...], *, as_of_utc: str = COMPILED_AT_UTC) -> dict:
    rows = []
    for product in products:
        for station in canonical_station_ids():
            for lead in ("d0", "d1", "d2"):
                rows.append(make_coverage_row(
                    station_id=station,
                    product_id=product,
                    provider_id="TEST",
                    model_family="global_ensemble",
                    independence_family_id="TEST_" + product,
                    source_class="native_official",
                    lead_bucket=lead,
                    run_cycle=None,
                    as_of_utc=as_of_utc,
                    state="BLOCKED",
                    blockers=["DATA_MISSING"],
                    evidence_role="observational_candidate_not_oos",
                    strict_asof=False,
                    closed_awc_truth=False,
                    train_count=0,
                    holdout_count=0,
                    source_evidence_sha256=content_sha256({"product": product}),
                    raw_receipt_sha256=None,
                    oos_evidence_sha256=None,
                    single_system_artifact_sha256=None,
                    native_arrival_indexed=False,
                    formal_live_authority=False,
                    cross_selection_count=0,
                    cross_activation_count=0,
                    p0_count=0,
                ))
    return build_coverage_matrix(
        rows,
        expected_product_ids=products,
        as_of_utc=as_of_utc,
    )


def test_outer_builder_rebuilds_exact_21_product_coverage_and_validates_readback() -> None:
    result = build_pr05_pr09_coverage_matrix(
        pr05_matrix=_matrix(PR05_PRODUCTS),
        pr06_matrix=_matrix(PR06_PRODUCTS),
        pr07_matrix=_matrix(PR07_PRODUCTS),
        pr08_pr09_matrix=_matrix(PR08_PR09_PRODUCTS),
    )

    assert result["scope_count"] == EXPECTED_SCOPE_COUNT == 21 * expected_station_count() * 3
    assert result["product_ids"] == list(EXPECTED_PRODUCT_IDS)
    assert result["state_counts"] == {
        "DATA_READY": 0,
        "OOS_READY": 0,
        "CANDIDATE_ONLY": 0,
        "BLOCKED": EXPECTED_SCOPE_COUNT,
    }
    assert result["cross_selection_count"] == 0
    assert result["cross_activation_count"] == 0
    assert result["p0_count"] == 0
    assert validate_coverage_matrix(result) == result


def test_outer_builder_rejects_source_clock_or_product_contract_drift() -> None:
    with pytest.raises(ContractError, match="source_as_of_mismatch"):
        build_pr05_pr09_coverage_matrix(
            pr05_matrix=_matrix(PR05_PRODUCTS, as_of_utc="2026-08-03T21:08:01Z"),
            pr06_matrix=_matrix(PR06_PRODUCTS),
            pr07_matrix=_matrix(PR07_PRODUCTS),
            pr08_pr09_matrix=_matrix(PR08_PR09_PRODUCTS),
        )

    with pytest.raises(ContractError, match="source_product_set_mismatch"):
        build_pr05_pr09_coverage_matrix(
            pr05_matrix=_matrix(PR05_PRODUCTS[:-1]),
            pr06_matrix=_matrix(PR06_PRODUCTS),
            pr07_matrix=_matrix(PR07_PRODUCTS),
            pr08_pr09_matrix=_matrix(PR08_PR09_PRODUCTS),
        )


def test_cli_refuses_any_outer_clock_other_than_frozen_release_clock(tmp_path) -> None:
    with pytest.raises(ContractError, match="compiled_at_fixed"):
        main([
            "--compiled-at-utc",
            "2026-08-03T21:08:01Z",
            "--output",
            str(tmp_path / "coverage.json"),
        ])

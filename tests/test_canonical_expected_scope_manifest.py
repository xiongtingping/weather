from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.canonical_activation_release import (
    REQUIRED_SCOPE_TYPES,
    load_expected_scope_manifest,
)
from bayes_basket_target.canonical_expected_scope_manifest import (
    CanonicalExpectedScopeError,
    build_model_oos_scope_basis,
    build_scope_basis,
    compile_expected_scope_manifest,
)
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.model_system_coverage import (
    LEADS,
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-04T10:00:00Z"


def _matrix(products: tuple[str, ...] = ("GFS",)) -> dict:
    rows = []
    for product in products:
        for station in canonical_station_ids():
            for lead in LEADS:
                rows.append(
                    make_coverage_row(
                    station_id=station,
                    product_id=product,
                    provider_id=f"PROVIDER_{product}",
                    model_family="global_deterministic",
                    independence_family_id=f"DETERMINISTIC_{product}",
                    source_class="aggregator_republisher",
                    lead_bucket=lead,
                    run_cycle={
                        "d0": None,
                        "d1": "PREVIOUS_DAY1",
                        "d2": "PREVIOUS_DAY2",
                    }[lead],
                    as_of_utc="2026-08-03T21:08:00Z",
                    state="BLOCKED",
                    blockers=[
                        "RUN_CYCLE_MISMATCH" if lead == "d0" else "OOS_SAMPLE_SHORT"
                    ],
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
                    )
                )
    return build_coverage_matrix(
        rows, expected_product_ids=products, as_of_utc="2026-08-03T21:08:00Z"
    )


def _runtime_keys() -> list[dict]:
    return [
        {
            "scope_kind": "SERVICE_RELEASE",
            "natural_key": {
                "service_label": label,
                "launch_domain": "gui/501",
                "runtime_release_id": "runtime-release-1",
            },
        }
        for label in (
            "com.weather.weather_basket_yes",
            "com.weather.market_ws",
            "com.weather.clob_order_service",
        )
    ]


def test_model_basis_comes_from_frozen_matrix_not_observed_release() -> None:
    basis = build_model_oos_scope_basis(_matrix(), release_as_of_utc=AS_OF)
    assert basis["scope_type"] == "MODEL_OOS"
    assert len(basis["expected_keys"]) == expected_station_count() * 3
    assert {item["natural_key"]["lead_bucket"] for item in basis["expected_keys"]} == set(LEADS)
    assert basis["source_content_sha256s"] == [_matrix()["content_sha256"]]


def test_model_basis_rejects_source_after_release() -> None:
    with pytest.raises(CanonicalExpectedScopeError, match="source_after_release"):
        build_model_oos_scope_basis(
            _matrix(), release_as_of_utc="2026-08-03T20:00:00Z"
        )


def test_model_basis_keeps_complete_product_vector_when_family_key_collapses() -> None:
    basis = build_model_oos_scope_basis(
        _matrix(("ECMWF", "GFS")), release_as_of_utc=AS_OF
    )
    assert len(basis["expected_keys"]) == expected_station_count() * 3
    assert len(basis["expected_key_evidence"]) == expected_station_count() * 3
    assert all(
        [item["product_id"] for item in row["evidence"]["components"]]
        == ["ECMWF", "GFS"]
        for row in basis["expected_key_evidence"]
    )


def test_compile_requires_exact_eleven_independent_bases(tmp_path: Path) -> None:
    unresolved = content_sha256({"missing": "domain-producer"})
    bases = []
    for scope_type in REQUIRED_SCOPE_TYPES:
        keys = _runtime_keys() if scope_type == "RUNTIME" else []
        bases.append(
            build_scope_basis(
                scope_type=scope_type,
                as_of_utc=AS_OF,
                expected_keys=keys,
                source_content_sha256s=(content_sha256({"source": scope_type}),),
                unresolved_input_receipts=(unresolved,),
            )
        )
    manifest = compile_expected_scope_manifest(bases, as_of_utc=AS_OF)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded, _ = load_expected_scope_manifest(
        path,
        release_as_of_utc=AS_OF,
        canonical_station_ids=coordinate_registry_manifest()["station_ids"],
        canonical_registry_sha256=coordinate_registry_manifest()["registry_sha256"],
    )
    assert list(loaded["normalized_expected_keys"]) == list(REQUIRED_SCOPE_TYPES)
    assert all(
        loaded["normalized_scope_metadata"][scope]["unresolved_input_receipts"]
        == [unresolved]
        for scope in REQUIRED_SCOPE_TYPES
    )


def test_compile_rejects_missing_scope_basis() -> None:
    basis = build_model_oos_scope_basis(_matrix(), release_as_of_utc=AS_OF)
    with pytest.raises(CanonicalExpectedScopeError, match="basis_set_not_exact"):
        compile_expected_scope_manifest([basis], as_of_utc=AS_OF)


def test_runtime_basis_allows_exact_three_unresolved_services_without_fake_id() -> None:
    receipts = [content_sha256({"service": label}) for label in (
        "com.weather.weather_basket_yes",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    )]
    basis = build_scope_basis(
        scope_type="RUNTIME",
        as_of_utc=AS_OF,
        expected_keys=(),
        source_content_sha256s=(content_sha256({"source": "runtime"}),),
        unresolved_input_receipts=receipts,
    )
    assert basis["expected_keys"] == []
    assert basis["unresolved_input_receipts"] == sorted(receipts)


def test_runtime_basis_rejects_incomplete_unresolved_service_set() -> None:
    with pytest.raises(CanonicalExpectedScopeError, match="runtime_unresolved_services_invalid"):
        build_scope_basis(
            scope_type="RUNTIME",
            as_of_utc=AS_OF,
            expected_keys=(),
            source_content_sha256s=(content_sha256({"source": "runtime"}),),
            unresolved_input_receipts=(content_sha256({"service": "one"}),),
        )


def test_scope_basis_rejects_unproven_station() -> None:
    with pytest.raises(CanonicalExpectedScopeError, match="station_invalid"):
        build_scope_basis(
            scope_type="MODEL_OOS",
            as_of_utc=AS_OF,
            expected_keys=({
                "scope_kind": "BUILDER",
                "natural_key": {
                    "station_id": "ZZZZ",
                    "model_family": "global_deterministic",
                    "lead_bucket": "d1",
                    "run_cycle": "PREVIOUS_DAY1",
                },
            },),
            source_content_sha256s=("1" * 64,),
        )

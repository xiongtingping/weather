from __future__ import annotations

from bayes_basket_target.benchmark_model_coverage import (
    GLOBAL_DETERMINISTIC_PRODUCTS,
    REGIONAL_BENCHMARK_PRODUCTS,
    build_global_deterministic_coverage_rows,
    build_regional_benchmark_coverage_rows,
)
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.model_system_coverage import build_coverage_matrix, canonical_station_ids
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-03T21:08:00Z"


def _facts() -> list[dict]:
    result = []
    for product in GLOBAL_DETERMINISTIC_PRODUCTS:
        for station in canonical_station_ids():
            for lead, cycle in (("d1", "PREVIOUS_DAY1"), ("d2", "PREVIOUS_DAY2")):
                for day in range(1, 29):
                    identity = {
                        "station_id": station,
                        "model_id": product,
                        "model_family": "global_deterministic",
                        "provider_id": f"{product.lower()}_open_meteo",
                        "source_class": "aggregator_republisher",
                        "event_date": f"2026-07-{day:02d}",
                        "lead_bucket": lead,
                        "run_cycle": cycle,
                        "strict_asof": True,
                        "formal_live_authority": False,
                        "native_arrival_indexed": False,
                    }
                    result.append({**identity, "content_sha256": content_sha256(identity)})
    return result


def _d1_receipt() -> dict:
    identity = {
        "schema": "bayes_basket_target_d1_canonical_build_v2",
        "station_statuses": [
            {"station_id": station, "train_count": 19, "holdout_count": 8}
            for station in canonical_station_ids()
        ],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _regional_audit() -> dict:
    identity = {
        "schema": "regional_model_candidate_closure_audit_v1",
        "models": [
            {
                "model_key": product,
                "geographic_scope": "regional",
                "source_class": "aggregator_republisher",
                "station_ids": list(canonical_station_ids()),
            }
            for product in REGIONAL_BENCHMARK_PRODUCTS
        ],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_pr05_matrix_is_exact_and_never_mints_p0() -> None:
    rows = (
        *build_global_deterministic_coverage_rows(
            _facts(), d1_build_receipt=_d1_receipt(), as_of_utc=AS_OF
        ),
        *build_regional_benchmark_coverage_rows(_regional_audit(), as_of_utc=AS_OF),
    )
    products = (*GLOBAL_DETERMINISTIC_PRODUCTS, *REGIONAL_BENCHMARK_PRODUCTS)
    matrix = build_coverage_matrix(rows, expected_product_ids=products, as_of_utc=AS_OF)
    assert matrix["scope_count"] == expected_station_count() * 3 * len(products)
    assert matrix["p0_count"] == 0
    assert matrix["cross_activation_count"] == 0
    assert matrix["state_counts"]["OOS_READY"] == 0
    assert all(row["run_cycle"] is None for row in rows if row["lead_bucket"] == "d0")
    assert all(
        row["blockers"] == ["RUN_CYCLE_MISMATCH"]
        for row in rows
        if row["lead_bucket"] == "d0"
    )

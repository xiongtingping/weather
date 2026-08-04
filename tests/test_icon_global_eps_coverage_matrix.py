from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.canonical_contract import TYPED_SCOPE_BLOCKERS
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.icon_global_eps_coverage_matrix import (
    D2_EPS,
    EU_EPS,
    GLOBAL_EPS,
    build_icon_global_eps_coverage_matrix,
    render_icon_global_eps_terminal_matrix,
)
from bayes_basket_target.model_system_coverage import (
    MATRIX_SCHEMA,
    canonical_station_ids,
    validate_coverage_row,
)


AS_OF = "2026-08-03T21:08:00Z"


def _official_readiness(*, ready_cohorts: int = 0) -> dict[str, object]:
    return {
        "schema": "dwd_icon_global_eps_official_readiness_v1",
        "state": "BLOCKED_FAIL_CLOSED",
        "official_eps_products": {
            GLOBAL_EPS: {
                "source_role": "official_native",
                "station_member_cohort_ready_count": ready_cohorts,
                "native_adapter_execution_ready": ready_cohorts > 0,
                "blockers": ["no_complete_official_40_member_cohort"]
                if not ready_cohorts
                else [],
            }
        },
    }


def _strict_oos(
    *,
    native_arrival_index_used: bool = False,
    explicit_katl_d0: bool = False,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "dwd_icon_global_eps_official_strict_oos_v1",
        "state": "STRICT_OOS_CANDIDATES_NOT_BINDING",
        "as_of_utc": AS_OF,
        "product_id": GLOBAL_EPS,
        "formal_p0_eligible": False,
        "promotion_authority_generated": False,
        "source_authority": {
            "forecast_source_role": "official_native",
            "forecast_transport_provider_id": "DWD",
            "native_arrival_index_used": native_arrival_index_used,
            "open_meteo_fallback_used": False,
        },
        "readiness": [
            {
                "station_id": station,
                "lead_bucket": "*",
                "run_cycle": "*",
                "state": "PENDING_CLOSED_AWC_TRUTH",
                "training_case_count": 0,
                "strict_holdout_case_count": 0,
                "blockers": ["arrival lineage 缺失", "样本不足"],
            }
            for station in canonical_station_ids()
        ],
    }
    if explicit_katl_d0:
        readiness = identity["readiness"]
        assert isinstance(readiness, list)
        readiness.append(
            {
                "station_id": "KATL",
                "lead_bucket": "d0",
                "run_cycle": "00Z",
                "state": "STRICT_OOS_READY_NOT_BINDING",
                "training_case_count": 20,
                "strict_holdout_case_count": 8,
                "raw_receipt_sha256": "a" * 64,
                "blockers": [],
            }
        )
    return {**identity, "content_sha256": content_sha256(identity)}


def _daily_candidate() -> dict[str, object]:
    return {
        "schema": "dwd_icon_global_eps_official_daily_members_v1",
        "ensemble_system_id": GLOBAL_EPS,
        "member_count": 40,
        "station_count": 45,
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "source_authority": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
        },
    }


def _republisher_binding(*, system_id: str = GLOBAL_EPS) -> dict[str, object]:
    return {
        "schema": "bayes_basket_target_republisher_no_live_fallback_binding_v1",
        "authority": {
            "source_role": "aggregator_republisher",
            "transport_provider_id": "OPEN_METEO",
            "native_arrival_index_eligible": False,
            "native_formal_p0_eligible": False,
            "p1_eligible": False,
            "formal_live_authority": False,
        },
        "bindings": [
            {
                "ensemble_system_id": system_id,
                "station_id": "KATL",
                "source_role": "aggregator_republisher",
                "native_arrival_index_eligible": False,
                "native_formal_p0_eligible": False,
                "p1_eligible": False,
                "formal_live_authority": False,
            }
        ],
    }


def test_final_output_is_exact_shared_45_by_three_matrix_with_unique_blockers() -> None:
    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_official_readiness(),
        strict_oos=_strict_oos(),
        daily_archives=[_daily_candidate()],
        republisher_binding=_republisher_binding(),
    )

    assert matrix["schema"] == MATRIX_SCHEMA
    assert matrix["scope_count"] == 135
    assert matrix["product_ids"] == [GLOBAL_EPS]
    assert matrix["cross_selection_count"] == 0
    assert matrix["cross_activation_count"] == 0
    assert matrix["p0_count"] == 0
    assert matrix["state_counts"]["BLOCKED"] == 135
    assert all(validate_coverage_row(row) == row for row in matrix["rows"])
    assert all(row["blockers"] == ["DATA_MISSING"] for row in matrix["rows"])
    assert all(len(row["blockers"]) == 1 for row in matrix["rows"])
    assert all(row["blockers"][0] in TYPED_SCOPE_BLOCKERS for row in matrix["rows"])

    terminal = render_icon_global_eps_terminal_matrix(matrix)
    assert len(terminal.splitlines()) == 48
    assert "KATL | BLOCKED/DATA_MISSING" in terminal
    assert terminal.endswith("cross_selection_count=0; cross_activation_count=0; p0_count=0")


def test_exact_native_strict_scope_can_be_oos_ready_without_p0_or_cross_activation() -> None:
    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_official_readiness(ready_cohorts=1),
        strict_oos=_strict_oos(
            native_arrival_index_used=True,
            explicit_katl_d0=True,
        ),
    )
    katl_d0 = next(
        row
        for row in matrix["rows"]
        if row["station_id"] == "KATL" and row["lead_bucket"] == "d0"
    )

    assert katl_d0["state"] == "OOS_READY"
    assert katl_d0["blockers"] == []
    assert katl_d0["train_count"] == 20
    assert katl_d0["holdout_count"] == 8
    assert katl_d0["cross_activation_count"] == 0
    assert katl_d0["p0_count"] == 0


def test_missing_explicit_lead_or_run_cycle_is_canonical_cycle_mismatch_not_inferred() -> None:
    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_official_readiness(ready_cohorts=1),
        strict_oos=_strict_oos(native_arrival_index_used=True),
    )

    assert matrix["state_counts"]["BLOCKED"] == 135
    assert all(row["run_cycle"] is None for row in matrix["rows"])
    assert all(row["blockers"] == ["RUN_CYCLE_MISMATCH"] for row in matrix["rows"])


@pytest.mark.parametrize("regional_system", [EU_EPS, D2_EPS])
def test_regional_republisher_is_validated_but_never_added_as_a_global_product(
    regional_system: str,
) -> None:
    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_official_readiness(),
        strict_oos=_strict_oos(),
        republisher_binding=_republisher_binding(system_id=regional_system),
    )

    assert matrix["product_ids"] == [GLOBAL_EPS]
    assert matrix["scope_count"] == 135
    assert all(row["product_id"] == GLOBAL_EPS for row in matrix["rows"])


def test_republisher_cannot_claim_native_or_formal_authority() -> None:
    binding = _republisher_binding()
    authority = binding["authority"]
    assert isinstance(authority, dict)
    authority["native_arrival_index_eligible"] = True

    with pytest.raises(ContractError, match="icon_eps_coverage_republisher_authority_invalid"):
        build_icon_global_eps_coverage_matrix(
            official_readiness=_official_readiness(),
            strict_oos=_strict_oos(),
            republisher_binding=binding,
        )


def test_daily_archive_cannot_relabel_eu_as_global_eps() -> None:
    daily = deepcopy(_daily_candidate())
    daily["ensemble_system_id"] = EU_EPS

    with pytest.raises(ContractError, match="icon_eps_coverage_daily_archive_identity_invalid"):
        build_icon_global_eps_coverage_matrix(
            official_readiness=_official_readiness(),
            strict_oos=_strict_oos(),
            daily_archives=[daily],
        )


def test_unlabelled_icon_republisher_gets_no_schema_alias_or_global_product() -> None:
    binding = _republisher_binding()
    row = binding["bindings"][0]
    assert isinstance(row, dict)
    del row["ensemble_system_id"]
    row["model_id"] = "ICON"

    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=_official_readiness(),
        strict_oos=_strict_oos(),
        republisher_binding=binding,
    )

    assert matrix["scope_count"] == 135
    assert all(row["product_id"] == GLOBAL_EPS for row in matrix["rows"])

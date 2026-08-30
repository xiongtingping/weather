from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_system_coverage import (
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
    validate_coverage_matrix,
    validate_coverage_row,
)
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-03T21:08:00Z"


def _row(station: str, lead: str, *, state: str = "BLOCKED", blocker: str | None = "DATA_MISSING") -> dict:
    ready = state == "OOS_READY"
    return make_coverage_row(
        station_id=station,
        product_id="TEST_SYSTEM",
        provider_id="TEST_PROVIDER",
        model_family="global_ensemble",
        independence_family_id="TEST_FAMILY",
        source_class="native_official",
        lead_bucket=lead,
        run_cycle="18Z",
        as_of_utc=AS_OF,
        state=state,
        blockers=[] if blocker is None else [blocker],
        evidence_role="historical_strict_oos",
        strict_asof=ready,
        closed_awc_truth=ready,
        train_count=20 if ready else 0,
        holdout_count=8 if ready else 0,
        source_evidence_sha256="d" * 64,
        raw_receipt_sha256="a" * 64 if ready else None,
        oos_evidence_sha256="b" * 64 if ready else None,
        single_system_artifact_sha256="c" * 64 if ready else None,
        native_arrival_indexed=ready,
        formal_live_authority=False,
        cross_selection_count=0,
        cross_activation_count=0,
        p0_count=0,
    )


def test_exact_45x3_product_matrix_and_zero_cross_authority() -> None:
    rows = [
        _row(station, lead, state="OOS_READY", blocker=None)
        for station in canonical_station_ids()
        for lead in ("d0", "d1", "d2")
    ]
    matrix = build_coverage_matrix(rows, expected_product_ids=["TEST_SYSTEM"], as_of_utc=AS_OF)
    assert matrix["scope_count"] == expected_station_count() * 3
    assert matrix["state_counts"]["OOS_READY"] == expected_station_count() * 3
    assert matrix["cross_selection_count"] == 0
    assert matrix["cross_activation_count"] == 0
    assert matrix["p0_count"] == 0


def test_missing_station_scope_fails_closed() -> None:
    rows = [
        _row(station, lead)
        for station in canonical_station_ids()
        for lead in ("d0", "d1", "d2")
    ]
    rows.pop()
    with pytest.raises(ContractError, match="scope_not_exact_45x3xproduct"):
        build_coverage_matrix(rows, expected_product_ids=["TEST_SYSTEM"], as_of_utc=AS_OF)


def test_matrix_validator_rejects_summary_tamper() -> None:
    rows = [_row(station, lead) for station in canonical_station_ids() for lead in ("d0", "d1", "d2")]
    matrix = build_coverage_matrix(rows, expected_product_ids=["TEST_SYSTEM"], as_of_utc=AS_OF)
    assert validate_coverage_matrix(matrix) == matrix
    matrix["scope_count"] = 134
    with pytest.raises(ContractError, match="identity_mismatch"):
        validate_coverage_matrix(matrix)


def test_forward_cannot_count_as_oos() -> None:
    row = _row("KATL", "d0", state="OOS_READY", blocker=None)
    row["evidence_role"] = "observational_candidate_not_oos"
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="forward_counted_as_oos|forward_marked_oos_ready"):
        validate_coverage_row(row)


def test_republisher_cannot_claim_native_arrival_or_formal_authority() -> None:
    row = _row("KATL", "d0")
    row["source_class"] = "aggregator_republisher"
    row["native_arrival_indexed"] = True
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="republisher_native_arrival_forbidden"):
        validate_coverage_row(row)

    row = _row("KATL", "d0")
    row["formal_live_authority"] = True
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="formal_live_authority_forbidden"):
        validate_coverage_row(row)


def test_cross_activation_and_p0_are_forbidden_in_pr05_to_pr09() -> None:
    row = _row("KATL", "d2", state="OOS_READY", blocker=None)
    row["cross_activation_count"] = 1
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="cross_or_p0_authority_forbidden"):
        validate_coverage_row(row)


def test_native_oos_ready_requires_raw_receipt_and_arrival_index() -> None:
    row = _row("KATL", "d2", state="OOS_READY", blocker=None)
    row["native_arrival_indexed"] = False
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="native_oos_ready_arrival_missing"):
        validate_coverage_row(row)


def test_missing_run_cycle_requires_exact_cycle_blocker() -> None:
    row = _row("KATL", "d1")
    row["run_cycle"] = None
    row["blockers"] = ["OOS_SAMPLE_SHORT"]
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="missing_cycle_without_exact_blocker"):
        validate_coverage_row(row)

    row["blockers"] = ["RUN_CYCLE_MISMATCH"]
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    assert validate_coverage_row(row)["run_cycle"] is None

    row["blockers"] = ["DATA_MISSING"]
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    assert validate_coverage_row(row)["run_cycle"] is None


def test_alias_unknown_field_and_nonunique_blocker_are_rejected() -> None:
    row = _row("KATL", "d1")
    row["lead"] = row["lead_bucket"]
    with pytest.raises(ContractError, match="unknown_field"):
        validate_coverage_row(row)

    row = _row("KATL", "d1")
    row["blockers"] = ["DATA_MISSING", "OOS_SAMPLE_SHORT"]
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="terminal_blocker_not_unique"):
        validate_coverage_row(row)

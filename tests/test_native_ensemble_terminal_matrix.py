from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_system_coverage import (
    MATRIX_SCHEMA,
    canonical_station_ids,
    validate_coverage_row,
)
from bayes_basket_target.native_ensemble_terminal_matrix import (
    AUDIT_SCHEMA,
    build_pr08_pr09_terminal_matrix,
)


STATIONS = canonical_station_ids()


def _readiness(
    *,
    system_id: str,
    run_cycle: str,
    ready: bool,
    include_wrong_d0: bool = False,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if include_wrong_d0:
        rows.append({
            "ensemble_system_id": system_id,
            "lead_bucket": "d0",
            "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            "state": "ACCUMULATING_STRICT_OOS",
            "case_count": 4,
            "training_case_count": 4,
            "strict_holdout_case_count": 0,
        })
    if ready:
        rows.append({
            "ensemble_system_id": system_id,
            "lead_bucket": "d2",
            "run_cycle": run_cycle,
            "state": "OOS_READY",
            "case_count": 28,
            "train_n": 20,
            "holdout_n": 8,
        })
    else:
        rows.append({
            "ensemble_system_id": system_id,
            "lead_bucket": "d2",
            "run_cycle": run_cycle,
            "state": "ACCUMULATING_STRICT_OOS",
            "case_count": 27,
            "training_case_count": 19,
            "strict_holdout_case_count": 8,
        })
    return rows


def _audit(
    *,
    system_id: str,
    run_cycle: str,
    ready_count: int,
    include_wrong_d0: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": AUDIT_SCHEMA,
        "as_of_utc": "2026-08-03T21:08:00Z",
        "station_count": 45,
        "min_train_days": 20,
        "min_holdout_days": 8,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
        "reports": [
            {
                "station_id": station_id,
                "readiness": _readiness(
                    system_id=system_id,
                    run_cycle=run_cycle,
                    ready=index < ready_count,
                    include_wrong_d0=include_wrong_d0,
                ),
            }
            for index, station_id in enumerate(STATIONS)
        ],
    }
    payload["content_sha256"] = content_sha256(payload)
    return payload


def _matrix() -> dict[str, object]:
    return build_pr08_pr09_terminal_matrix(
        gefs_audit=_audit(
            system_id="NOAA_GEFS_05",
            run_cycle="18Z",
            ready_count=45,
            include_wrong_d0=True,
        ),
        geps_audit=_audit(
            system_id="ECCC_GEM_GLOBAL_ENS",
            run_cycle="00Z",
            ready_count=10,
        ),
        station_ids=STATIONS,
        gefs_audit_pointer="output/audits/native_ensemble_oos/gefs.json",
        geps_audit_pointer="output/audits/native_ensemble_oos/geps.json",
        compiled_at_utc="2026-08-04T00:00:00Z",
    )


def test_terminal_adapter_returns_only_shared_45_by_three_product_matrix() -> None:
    matrix = _matrix()

    assert matrix["schema"] == MATRIX_SCHEMA
    assert matrix["scope_count"] == 45 * 3 * 2
    assert matrix["state_counts"] == {
        "DATA_READY": 0,
        "OOS_READY": 0,
        "CANDIDATE_ONLY": 0,
        "BLOCKED": 270,
    }
    assert matrix["cross_selection_count"] == 0
    assert matrix["cross_activation_count"] == 0
    assert matrix["p0_count"] == 0

    rows = matrix["rows"]
    assert len({
        (row["station_id"], row["product_id"], row["lead_bucket"])
        for row in rows
    }) == 270
    assert all(validate_coverage_row(row) == row for row in rows)
    assert all(row["cross_selection_count"] == 0 for row in rows)
    assert all(row["cross_activation_count"] == 0 for row in rows)
    assert all(row["p0_count"] == 0 for row in rows)

    gefs_d0 = next(
        row for row in rows
        if row["station_id"] == STATIONS[0]
        and row["product_id"] == "NOAA_GEFS_05"
        and row["lead_bucket"] == "d0"
    )
    assert gefs_d0["state"] == "BLOCKED"
    assert gefs_d0["blockers"] == ["RUN_CYCLE_MISMATCH"]
    assert gefs_d0["run_cycle"] is None

    gefs_d2 = next(
        row for row in rows
        if row["station_id"] == STATIONS[0]
        and row["product_id"] == "NOAA_GEFS_05"
        and row["lead_bucket"] == "d2"
    )
    assert gefs_d2["state"] == "BLOCKED"
    assert gefs_d2["blockers"] == ["LINEAGE_MISMATCH"]
    assert gefs_d2["evidence_role"] == "observational_candidate_not_oos"
    assert gefs_d2["strict_asof"] is False
    assert gefs_d2["closed_awc_truth"] is False
    assert gefs_d2["oos_evidence_sha256"] is None
    assert gefs_d2["single_system_artifact_sha256"] is None

    geps_short = next(
        row for row in rows
        if row["station_id"] == STATIONS[10]
        and row["product_id"] == "ECCC_GEM_GLOBAL_ENS"
        and row["lead_bucket"] == "d2"
    )
    assert geps_short["state"] == "BLOCKED"
    assert geps_short["blockers"] == ["LINEAGE_MISMATCH"]
    assert geps_short["train_count"] == 0
    assert geps_short["holdout_count"] == 0


def test_terminal_adapter_rejects_duplicate_or_noncanonical_audit_station_set() -> None:
    gefs = _audit(
        system_id="NOAA_GEFS_05",
        run_cycle="18Z",
        ready_count=45,
    )
    duplicate = deepcopy(gefs)
    duplicate["reports"][44]["station_id"] = STATIONS[0]
    duplicate["content_sha256"] = content_sha256({
        key: value for key, value in duplicate.items() if key != "content_sha256"
    })

    with pytest.raises(ContractError, match="station_report_duplicate"):
        build_pr08_pr09_terminal_matrix(
            gefs_audit=duplicate,
            geps_audit=_audit(
                system_id="ECCC_GEM_GLOBAL_ENS",
                run_cycle="00Z",
                ready_count=10,
            ),
            station_ids=STATIONS,
            gefs_audit_pointer="gefs.json",
            geps_audit_pointer="geps.json",
            compiled_at_utc="2026-08-04T00:00:00Z",
        )


def test_d2_wrong_cycle_or_ready_count_conflict_remain_nonready() -> None:
    matrix = build_pr08_pr09_terminal_matrix(
        gefs_audit=_audit(
            system_id="NOAA_GEFS_05",
            run_cycle="18Z",
            ready_count=45,
        ),
        geps_audit=_audit(
            system_id="ECCC_GEM_GLOBAL_ENS",
            run_cycle="18Z",
            ready_count=45,
        ),
        station_ids=STATIONS,
        gefs_audit_pointer="gefs.json",
        geps_audit_pointer="geps.json",
        compiled_at_utc="2026-08-04T00:00:00Z",
    )
    wrong_cycle = next(
        row for row in matrix["rows"]
        if row["station_id"] == STATIONS[0]
        and row["product_id"] == "ECCC_GEM_GLOBAL_ENS"
        and row["lead_bucket"] == "d2"
    )
    assert wrong_cycle["state"] == "BLOCKED"
    assert wrong_cycle["blockers"] == ["RUN_CYCLE_MISMATCH"]

    malformed = _audit(
        system_id="ECCC_GEM_GLOBAL_ENS",
        run_cycle="00Z",
        ready_count=45,
    )
    malformed["reports"][0]["readiness"][-1]["case_count"] = 27
    malformed["content_sha256"] = content_sha256({
        key: value for key, value in malformed.items() if key != "content_sha256"
    })
    conflicted = build_pr08_pr09_terminal_matrix(
        gefs_audit=_audit(
            system_id="NOAA_GEFS_05",
            run_cycle="18Z",
            ready_count=45,
        ),
        geps_audit=malformed,
        station_ids=STATIONS,
        gefs_audit_pointer="gefs.json",
        geps_audit_pointer="geps.json",
        compiled_at_utc="2026-08-04T00:00:00Z",
    )
    conflict = next(
        row for row in conflicted["rows"]
        if row["station_id"] == STATIONS[0]
        and row["product_id"] == "ECCC_GEM_GLOBAL_ENS"
        and row["lead_bucket"] == "d2"
    )
    assert conflict["state"] == "BLOCKED"
    assert conflict["blockers"] == ["LINEAGE_MISMATCH"]


def test_shared_validator_rejects_cross_authority_after_rehash() -> None:
    row = deepcopy(_matrix()["rows"][0])
    row["cross_activation_count"] = 1
    row["content_sha256"] = content_sha256({
        key: value for key, value in row.items() if key != "content_sha256"
    })

    with pytest.raises(ContractError, match="cross_or_p0_authority_forbidden"):
        validate_coverage_row(row)

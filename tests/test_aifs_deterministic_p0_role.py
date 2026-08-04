from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.aifs_deterministic_authority import (
    build_aifs_deterministic_promotion_receipt,
    issue_aifs_deterministic_provider_authority,
)
from bayes_basket_target.aifs_deterministic_oos import (
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    build_aifs_deterministic_strict_oos_candidate,
    build_aifs_deterministic_training_facts,
)
from bayes_basket_target.aifs_deterministic_p0_role import (
    INDEPENDENT_FAMILY_P0_AUTHORITY_SCHEMA,
    SELECTION_SCHEMA,
    build_aifs_deterministic_p0_role_selection,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from tests.test_aifs_deterministic_oos import _inputs


def _aifs_authority():
    curves, arrivals, truths = _inputs(
        count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    )
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    report = build_aifs_deterministic_strict_oos_candidate(batch)
    candidate = report["candidates"][0]
    promotion = build_aifs_deterministic_promotion_receipt(
        training_batch=batch,
        strict_oos_report=report,
        candidate_content_sha256=candidate["content_sha256"],
        decided_at_utc="2026-07-31T01:00:00Z",
        decision_actor="p0-role-test-gate",
    )
    authority = issue_aifs_deterministic_provider_authority(
        training_batch=batch,
        strict_oos_report=report,
        promotion_receipt=promotion,
        valid_from_utc="2026-07-31T01:00:00Z",
        valid_until_utc="2026-08-30T01:00:00Z",
    )
    return batch, report, promotion, authority


def _independent_authority(aifs: dict[str, object], *, family: str = "NOAA") -> dict[str, object]:
    identity = {
        "schema": INDEPENDENT_FAMILY_P0_AUTHORITY_SCHEMA,
        "authority_id": "official-gefs-test-authority",
        "provider_id": "noaa_gefs_official",
        "product_id": "NOAA_GEFS_05",
        "model_id": "GFS",
        "publication_authority": "official_native",
        "forecast_role": "NATIVE_ENSEMBLE",
        "independence_family_id": family,
        "formal_oos_state": "FORMAL_P0_READY",
        "formal_provider_eligible": True,
        "formal_p0_role_eligible": True,
        "native_member_count": 31,
        "formal_oos_artifact_sha256": "a" * 64,
        "formal_p0_authority_sha256": "b" * 64,
        "native_member_evidence_sha256": "c" * 64,
        "station_id": aifs["station_id"],
        "target_timezone": aifs["target_timezone"],
        "season_bin": aifs["season_bin"],
        "lead_bucket": aifs["lead_bucket"],
        "run_cycle": aifs["run_cycle"],
        "settlement_definition": "official_daily_tmax_c",
        "valid_from_utc": "2026-07-31T00:00:00Z",
        "valid_until_utc": "2026-08-30T01:00:00Z",
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_aifs_selects_one_ecmwf_role_with_one_independent_native_family() -> None:
    batch, report, promotion, aifs = _aifs_authority()
    other = _independent_authority(aifs)
    result = build_aifs_deterministic_p0_role_selection(
        aifs_training_batch=batch,
        aifs_strict_oos_report=report,
        aifs_promotion_receipt=promotion,
        aifs_provider_authority=aifs,
        independent_family_p0_authority=other,
        station_id=aifs["station_id"],
        target_timezone=aifs["target_timezone"],
        season_bin=aifs["season_bin"],
        lead_bucket=aifs["lead_bucket"],
        run_cycle=aifs["run_cycle"],
        settlement_definition="official_daily_tmax_c",
        selected_at_utc="2026-08-01T00:00:00Z",
    )

    assert result["schema"] == SELECTION_SCHEMA
    assert result["state"] == "P0_ROLE_SELECTION_ADAPTER_READY_NOT_COMPILED"
    assert result["independent_family_count"] == 2
    assert result["role_identity"]["ecmwf_role"]["forecast_role"] == "DETERMINISTIC_SINGLE"
    assert result["role_identity"]["independent_role"]["forecast_role"] == "NATIVE_ENSEMBLE"
    assert result["formal_p0_role_eligible"] is True
    assert result["formal_p0_probability_generated"] is False
    assert result["boundary"]["writes_native_ensemble_tables"] is False
    assert result["boundary"]["writes_probability_materialization"] is False


def test_same_ecmwf_family_cannot_be_used_as_a_second_family() -> None:
    batch, report, promotion, aifs = _aifs_authority()
    other = _independent_authority(aifs, family="ECMWF")
    with pytest.raises(ContractError, match="independent_identity_invalid"):
        build_aifs_deterministic_p0_role_selection(
            aifs_training_batch=batch,
            aifs_strict_oos_report=report,
            aifs_promotion_receipt=promotion,
            aifs_provider_authority=aifs,
            independent_family_p0_authority=other,
            station_id=aifs["station_id"],
            target_timezone=aifs["target_timezone"],
            season_bin=aifs["season_bin"],
            lead_bucket=aifs["lead_bucket"],
            run_cycle=aifs["run_cycle"],
            settlement_definition="official_daily_tmax_c",
            selected_at_utc="2026-08-01T00:00:00Z",
        )


def test_scope_tamper_or_stale_second_authority_fails_closed() -> None:
    batch, report, promotion, aifs = _aifs_authority()
    other = _independent_authority(aifs)
    stale = deepcopy(other)
    stale["valid_until_utc"] = "2026-07-31T01:00:00Z"
    stale["content_sha256"] = content_sha256({
        key: value for key, value in stale.items() if key != "content_sha256"
    })
    with pytest.raises(ContractError, match="independent_authority_stale"):
        build_aifs_deterministic_p0_role_selection(
            aifs_training_batch=batch,
            aifs_strict_oos_report=report,
            aifs_promotion_receipt=promotion,
            aifs_provider_authority=aifs,
            independent_family_p0_authority=stale,
            station_id=aifs["station_id"],
            target_timezone=aifs["target_timezone"],
            season_bin=aifs["season_bin"],
            lead_bucket=aifs["lead_bucket"],
            run_cycle=aifs["run_cycle"],
            settlement_definition="official_daily_tmax_c",
            selected_at_utc="2026-08-01T00:00:00Z",
        )


def test_adapter_never_accepts_a_role_scope_drift_as_probability_authority() -> None:
    batch, report, promotion, aifs = _aifs_authority()
    other = _independent_authority(aifs)
    with pytest.raises(ContractError, match="aifs_scope_mismatch"):
        build_aifs_deterministic_p0_role_selection(
            aifs_training_batch=batch,
            aifs_strict_oos_report=report,
            aifs_promotion_receipt=promotion,
            aifs_provider_authority=aifs,
            independent_family_p0_authority=other,
            station_id=aifs["station_id"],
            target_timezone=aifs["target_timezone"],
            season_bin="DJF",
            lead_bucket=aifs["lead_bucket"],
            run_cycle=aifs["run_cycle"],
            settlement_definition="official_daily_tmax_c",
            selected_at_utc="2026-08-01T00:00:00Z",
        )

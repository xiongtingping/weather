from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.aifs_deterministic_authority import (
    PROMOTION_RECEIPT_SCHEMA,
    PROVIDER_AUTHORITY_SCHEMA,
    build_aifs_deterministic_promotion_receipt,
    issue_aifs_deterministic_provider_authority,
)
from bayes_basket_target.aifs_deterministic_oos import (
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    build_aifs_deterministic_strict_oos_candidate,
    build_aifs_deterministic_training_facts,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from tests.test_aifs_deterministic_oos import _inputs


def _evidence():
    curves, arrivals, truths = _inputs(
        count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    )
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    report = build_aifs_deterministic_strict_oos_candidate(batch)
    candidate = report["candidates"][0]
    assert candidate["oos_state"] == "STRICT_OOS_PASS"
    return batch, report, candidate


def _rejected_evidence():
    curves, arrivals, truths = _inputs(
        count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    )
    # Make only the eight final holdouts bad.  The earlier facts remain a
    # valid rolling-origin calibration set, so this is a genuine OOS rejection
    # rather than an invalid/tampered report.
    for curve in curves[-MIN_HOLDOUT_DAYS:]:
        for point in curve["points"]:
            point["temperature_2m_c"] += 35.0
        curve["forecast_daily_max_c"] += 35.0
        identity = {
            "station_id": curve["station_id"],
            "event_date": curve["event_date"],
            "provider_id": curve["provider_id"],
            "model_id": curve["model_id"],
            "model_run_initialization_utc": curve["model_run_initialization_utc"],
            "points": curve["points"],
            "coverage": curve["coverage"],
            "target_timezone": curve["target_timezone"],
            "blockers": curve["blockers"],
        }
        curve["curve_sha256"] = content_sha256(identity)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    report = build_aifs_deterministic_strict_oos_candidate(batch)
    candidate = report["candidates"][0]
    assert candidate["oos_state"] == "STRICT_OOS_REJECTED"
    return batch, report, candidate


def test_passing_closed_awc_oos_promotes_only_provider_authority() -> None:
    batch, report, candidate = _evidence()
    receipt = build_aifs_deterministic_promotion_receipt(
        training_batch=batch,
        strict_oos_report=report,
        candidate_content_sha256=candidate["content_sha256"],
        decided_at_utc="2026-07-31T01:00:00Z",
        decision_actor="aifs-deterministic-formal-gate",
    )
    authority = issue_aifs_deterministic_provider_authority(
        training_batch=batch,
        strict_oos_report=report,
        promotion_receipt=receipt,
        valid_from_utc="2026-07-31T01:00:00Z",
        valid_until_utc="2026-08-30T01:00:00Z",
    )

    assert receipt["schema"] == PROMOTION_RECEIPT_SCHEMA
    assert receipt["decision"] == "PROMOTED"
    assert authority["schema"] == PROVIDER_AUTHORITY_SCHEMA
    assert authority["formal_provider_eligible"] is True
    assert authority["formal_p0_role_eligible"] is False
    assert authority["provider_lineage"]["forecast_role"] == "DETERMINISTIC_SINGLE"
    assert authority["provider_lineage"]["same_independence_family_as_ifs"] is True
    assert authority["provider_lineage"]["counts_as_distinct_independence_family"] is False
    assert authority["native_ensemble_member_tables_prohibited"] is True


def test_rejected_oos_receipt_cannot_issue_authority() -> None:
    batch, report, candidate = _rejected_evidence()
    receipt = build_aifs_deterministic_promotion_receipt(
        training_batch=batch,
        strict_oos_report=report,
        candidate_content_sha256=candidate["content_sha256"],
        decided_at_utc="2026-07-31T01:00:00Z",
        decision_actor="aifs-deterministic-formal-gate",
    )

    assert receipt["decision"] == "REJECTED"
    with pytest.raises(ContractError, match="aifs_deterministic_authority_promotion_not_passing"):
        issue_aifs_deterministic_provider_authority(
            training_batch=batch,
            strict_oos_report=report,
            promotion_receipt=receipt,
            valid_from_utc="2026-07-31T01:00:00Z",
            valid_until_utc="2026-08-30T01:00:00Z",
        )


def test_tampered_report_cannot_manufacture_a_promotion() -> None:
    batch, report, candidate = _evidence()
    forged = deepcopy(report)
    forged["candidates"][0]["oos_state"] = "STRICT_OOS_REJECTED"

    with pytest.raises(ContractError, match="aifs_deterministic_authority_oos_report_sha_mismatch"):
        build_aifs_deterministic_promotion_receipt(
            training_batch=batch,
            strict_oos_report=forged,
            candidate_content_sha256=candidate["content_sha256"],
            decided_at_utc="2026-07-31T01:00:00Z",
            decision_actor="aifs-deterministic-formal-gate",
        )


def test_authority_rejects_receipt_mutation_and_cannot_be_backdated() -> None:
    batch, report, candidate = _evidence()
    receipt = build_aifs_deterministic_promotion_receipt(
        training_batch=batch,
        strict_oos_report=report,
        candidate_content_sha256=candidate["content_sha256"],
        decided_at_utc="2026-07-31T01:00:00Z",
        decision_actor="aifs-deterministic-formal-gate",
    )
    forged = deepcopy(receipt)
    forged["decision"] = "REJECTED"
    with pytest.raises(ContractError, match="aifs_deterministic_authority_receipt_sha_mismatch"):
        issue_aifs_deterministic_provider_authority(
            training_batch=batch,
            strict_oos_report=report,
            promotion_receipt=forged,
            valid_from_utc="2026-07-31T01:00:00Z",
            valid_until_utc="2026-08-30T01:00:00Z",
        )
    with pytest.raises(ContractError, match="aifs_deterministic_authority_validity_invalid"):
        issue_aifs_deterministic_provider_authority(
            training_batch=batch,
            strict_oos_report=report,
            promotion_receipt=receipt,
            valid_from_utc="2026-07-31T00:59:59Z",
            valid_until_utc="2026-08-30T01:00:00Z",
        )

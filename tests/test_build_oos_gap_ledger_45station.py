from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_oos_gap_ledger_45station import (
    BLOCKER_TYPES,
    P1_AUDIT_SCHEMA,
    P1_REPAIR_SCHEMA,
    _evidence_role_for_event_date,
    _deterministic_republisher_records,
    _record,
    _validate_p1_repair_evidence_pair,
)


def _deterministic_artifact(*, event_date: str = "2026-07-28", as_of: str = "2026-08-02T12:00:00Z", future_event_count: int = 0, include_mmmx: bool = True):
    prediction = {
        "event_date": event_date,
        "actual_tmax_c": 24.0,
        "historical_oos_contract_id": "contract-1",
        "receipt_class": "HISTORICAL_ASOF_REPUBLISHER_RECEIPT",
        "temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
        "member_run_content_shas": ["b" * 64],
    }
    candidates = [{
        "station_id": "RKPK",
        "model_id": "AIFS",
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "content_sha256": "c" * 64,
        "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        "lead_bucket": "d0",
        "train_n": 20,
        "holdout_n": 8,
        "min_train_days": 20,
        "min_holdout_days": 8,
        "future_event_count": future_event_count,
        "oos_event_temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
        "predictions": [prediction],
    }]
    return {
        "schema": "deterministic_republisher_strict_oos_v1",
        "state": "READY_CANDIDATES",
        "as_of_utc": as_of,
        "model_id": "AIFS",
        "source_role": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "capture_receipt_sha256s": ["a" * 64],
        "candidates": candidates,
        "missing_truth_scopes": (
            [{"station_id": "MMMX", "event_date": "2026-07-23"}]
            if include_mmmx else []
        ),
    }


def _pair(*, audit_as_of: str = "2026-08-02T00:00:00.000Z", repair_as_of: str | None = None, repair_sha: str = "a" * 64):
    audit = {
        "schema": P1_AUDIT_SCHEMA,
        "as_of_utc": audit_as_of,
        "content_sha256": "a" * 64,
    }
    repair = {
        "schema": P1_REPAIR_SCHEMA,
        "as_of_utc": repair_as_of or audit_as_of,
        "audit_content_sha256": repair_sha,
    }
    return audit, repair


def test_p1_ledger_pair_requires_exact_asof_and_audit_hash() -> None:
    audit, repair = _pair()
    _validate_p1_repair_evidence_pair(audit, repair)

    audit, repair = _pair(repair_as_of="2026-08-02T00:00:00.001Z")
    with pytest.raises(RuntimeError, match="p1_evidence_pair_as_of_mismatch"):
        _validate_p1_repair_evidence_pair(audit, repair)

    audit, repair = _pair(repair_sha="b" * 64)
    with pytest.raises(RuntimeError, match="p1_evidence_pair_audit_hash_mismatch"):
        _validate_p1_repair_evidence_pair(audit, repair)


def test_future_evidence_is_non_oos_but_keeps_diagnostic_gap() -> None:
    row = _record(
        station_id="RKPK",
        model_family="ICON",
        model_id="DWD_ICON_GLOBAL_EPS_OFFICIAL",
        event_date="2026-08-03",
        source_class="official_native",
        native_arrival_verified=True,
        raw_receipt_hash_complete=True,
        awc_truth_closed=False,
        taf_revision_complete=False,
        state="OBSERVATIONAL_CANDIDATE_NOT_OOS",
        blockers=["truth 缺失", "样本不足"],
        evidence=["forward.json"],
        evidence_role="observational_candidate_not_oos",
    )
    assert row["evidence_role"] == "observational_candidate_not_oos"
    assert row["strict_oos_gate_applies"] is False
    assert row["future_event_not_oos"] is True
    assert set(row["blockers"]).issubset(BLOCKER_TYPES)
    assert row["hard_blockers"] == []
    assert set(row["observational_candidate_blockers"]) == set(row["blockers"])


def test_future_event_role_is_non_oos_for_every_per_event_audit_source() -> None:
    # The TAF revision-chain audit uses the same classifier as native/AIFS/
    # ICON rows.  A future correction diagnostic must remain visible without
    # becoming a historical 20+8 or TAF-24 OOS sample or hard-gate blocker.
    assert _evidence_role_for_event_date(
        event_date="2026-08-03", as_of_utc="2026-08-02T00:00:00Z"
    ) == "observational_candidate_not_oos"
    assert _evidence_role_for_event_date(
        event_date="2026-08-02", as_of_utc="2026-08-02T00:00:00Z"
    ) == "historical_strict_oos"


def test_historical_republisher_fallback_cannot_claim_native_p0_gate() -> None:
    row = _record(
        station_id="RKPK",
        model_family="GLOBAL",
        model_id="OPEN_METEO_ICON_GLOBAL",
        event_date="2026-08-01",
        source_class="aggregator_republisher",
        native_arrival_verified=False,
        raw_receipt_hash_complete=True,
        awc_truth_closed=True,
        taf_revision_complete=False,
        state="HISTORICAL_CALIBRATION_CANDIDATE",
        blockers=["arrival lineage 缺失"],
        evidence=["open-meteo-historical.json"],
    )
    # The row may be historical OOS/calibration evidence, but the ledger's
    # native gate remains unavailable to every aggregator_republisher row.
    row["historical_oos_calibration_fallback"] = True
    row["native_arrival_index_eligible"] = False
    row["native_p0_gate_eligible"] = False
    assert row["strict_oos_gate_applies"] is True
    assert row["historical_oos_calibration_fallback"] is True
    assert row["native_arrival_index_eligible"] is False
    assert row["native_p0_gate_eligible"] is False


def test_historical_utility_contract_gap_can_be_formal_only_not_no_live_hard_gate() -> None:
    row = _record(
        station_id="RKPK",
        model_family="STATION_POLICY",
        model_id="TARGET_RESERVE_CADENCE",
        event_date="*",
        source_class="target_owned_utility",
        native_arrival_verified=False,
        raw_receipt_hash_complete=True,
        awc_truth_closed=False,
        taf_revision_complete=False,
        state="BLOCKED_EVIDENCE",
        blockers=["utility 证据绑定缺失"],
        evidence=["scorecard.json"],
        # A real execution/settlement utility join is formal/live evidence,
        # not a substitute hard gate for historical no-live P0/P0-M/P1.
        strict_oos_gate_applies=False,
    )
    assert row["evidence_role"] == "historical_strict_oos"
    assert row["strict_oos_gate_applies"] is False
    assert row["hard_blockers"] == []


def test_open_meteo_deterministic_receipt_expands_historical_oos_without_native_authority() -> None:
    artifact = _deterministic_artifact()
    rows = _deterministic_republisher_records(
        artifact,
        artifact_path=Path("open_meteo.json"),
        stations=("RKPK", "MMMX"),
    )
    rkpk = next(row for row in rows if row["station_id"] == "RKPK")
    assert rkpk["source_class"] == "aggregator_republisher"
    assert rkpk["source_role"] == "aggregator_republisher"
    assert rkpk["evidence_role"] == "historical_strict_oos"
    assert rkpk["strict_oos_gate_applies"] is True
    assert rkpk["train_count"] == 20
    assert rkpk["holdout_count"] == 8
    assert rkpk["native_arrival_verified"] is False
    assert rkpk["native_arrival_index_eligible"] is False
    assert rkpk["native_p0_gate_eligible"] is False
    assert rkpk["formal_p0_eligible"] is False
    assert rkpk["formal_live_eligible"] is False
    assert rkpk["p1_eligible"] is False
    missing = next(row for row in rows if row["station_id"] == "MMMX")
    assert missing["train_count"] == 0
    assert missing["holdout_count"] == 0
    assert "truth 缺失" in missing["blockers"]
    assert missing["evidence_role"] == "historical_strict_oos"
    assert missing["strict_oos_gate_applies"] is False
    assert missing["future_event_not_oos"] is False


def test_open_meteo_future_prediction_is_observational_and_zero_oos_counts() -> None:
    artifact = _deterministic_artifact(event_date="2026-08-03", include_mmmx=False)
    rows = _deterministic_republisher_records(
        artifact,
        artifact_path=Path("open_meteo.json"),
        stations=("RKPK",),
    )
    row = rows[0]
    assert row["evidence_role"] == "observational_candidate_not_oos"
    assert row["strict_oos_gate_applies"] is False
    assert row["hard_blockers"] == []
    assert row["train_count"] == 0
    assert row["holdout_count"] == 0
    assert row["future_event_not_oos"] is True

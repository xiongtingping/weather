from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.probability_promotion import (
    BLOCKED_EVIDENCE,
    CANDIDATE_STATE,
    M3_11_SCHEMA,
    assert_probability_promotion_boundary,
    build_probability_promotion_candidate,
    validate_probability_promotion_candidate,
    validate_version_sequence,
)
from scripts.build_bayes_basket_target_probability_promotion_candidate import (
    build_from_payload,
)


AS_OF = "2026-03-01T00:00:00Z"
SCOPE = {
    "scope_keys": [
        {
            "station_id": "AAA",
            "lead_bucket": "d1",
            "run_cycle": "00Z",
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": "Asia/Shanghai",
        }
    ]
}


def _parent() -> dict[str, object]:
    return {
        "schema": "probability_parent_v1",
        "artifact_id": "parent-probability-v1",
        "content_sha256": "a" * 64,
        "station_id": "AAA",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Asia/Shanghai",
        "parameters": {"bias": 0.1, "scale": 1.0},
    }


def _equivalence() -> dict[str, object]:
    identity = {
        "schema": M3_11_SCHEMA,
        "state": "EQUIVALENT",
        "checkpoint_count": 16,
        "authoritative_checkpoint_id": "p1-aaa",
        "authoritative_stage": "P1",
        "canonical_replay_sha256": "b" * 64,
        "max_absolute_probability_difference": 0.0,
        "tolerance": 1e-9,
        "boundary": {
            "writes_production_db": False,
            "places_orders": False,
            "calls_order_gateway": False,
        },
    }
    return {**identity, "equivalence_sha256": content_sha256(identity)}


def _windows() -> dict[str, dict[str, object]]:
    return {
        "3": {"window_start_utc": "2026-01-01T00:00:00Z", "window_end_utc": "2026-01-04T00:00:00Z", "oos_metrics": {"brier_score": 0.10, "nll": 0.20, "crps": 0.12}},
        "7": {"window_start_utc": "2026-01-04T00:00:00Z", "window_end_utc": "2026-01-11T00:00:00Z", "oos_metrics": {"brier_score": 0.11, "nll": 0.21, "crps": 0.13}},
        "15": {"window_start_utc": "2026-01-11T00:00:00Z", "window_end_utc": "2026-01-26T00:00:00Z", "oos_metrics": {"brier_score": 0.12, "nll": 0.22, "crps": 0.14}},
        "30": {"window_start_utc": "2026-01-26T00:00:00Z", "window_end_utc": "2026-02-25T00:00:00Z", "oos_metrics": {"brier_score": 0.13, "nll": 0.23, "crps": 0.15}},
    }


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    starts = {3: 1, 7: 4, 15: 11, 30: 26}
    seasons = ("spring", "summer", "autumn", "winter")
    for window_days, day in starts.items():
        for offset, season in enumerate(seasons):
            hour = offset * 2
            rows.append(
                {
                    "row_id": f"{window_days}-{offset}",
                    "window_days": window_days,
                    "station_id": "AAA",
                    "lead_bucket": "d1",
                    "run_cycle": "00Z",
                    "settlement_definition": "official_daily_tmax_c",
                    "settlement_timezone": "Asia/Shanghai",
                    "season_bin": season,
                    "decision_at_utc": f"2026-01-{day:02d}T{hour:02d}:00:00Z",
                    "feature_as_of_utc": f"2026-01-{day:02d}T{max(0, hour - 1):02d}:00:00Z",
                    "evaluation_split": "OOS",
                }
            )
    return rows


def _build(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "parent_artifact": _parent(),
        "scope": SCOPE,
        "m3_11_equivalence_receipt": _equivalence(),
        "windows": _windows(),
        "as_of_utc": AS_OF,
        "oos_rows": _rows(),
        "candidate_parameters": {"bias": 0.1, "scale": 1.0},
        "artifact_id": "candidate-probability-v2",
        "cross_season_evidence": {"season_bins": ["spring", "summer", "autumn", "winter"]},
        "drift_evidence": {"detected": False, "max_parameter_delta": 0.0},
        "version_history": [1, 2],
    }
    values.update(overrides)
    return build_probability_promotion_candidate(**values)  # type: ignore[arg-type]


def test_valid_candidate_binds_all_windows_scope_parent_and_m311() -> None:
    result = _build()
    assert result["state"] == CANDIDATE_STATE
    assert result["evidence_state"] == "EVIDENCE_COMPLETE"
    assert result["blockers"] == []
    assert set(result["windows"]) == {"3", "7", "15", "30"}
    assert result["scope"]["scope_key_count"] == 1
    assert result["candidate_artifact"]["parent_artifact_sha256"] == "a" * 64
    assert result["m3_11_equivalence_sha256"] == _equivalence()["equivalence_sha256"]
    assert_probability_promotion_boundary(result)


def test_repeated_input_is_content_identical_and_order_independent() -> None:
    first = _build()
    shuffled = list(reversed(_rows()))
    second = _build(oos_rows=shuffled)
    assert first["content_sha256"] == second["content_sha256"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_compact_per_window_rows_use_the_same_strict_validator() -> None:
    windows = _windows()
    rows = _rows()
    for days, window in windows.items():
        window["rows"] = [row for row in rows if str(row["window_days"]) == days]
    result = _build(windows=windows, oos_rows=[])
    assert result["evidence_state"] == "EVIDENCE_COMPLETE"


def test_missing_sample_window_and_cross_season_are_blocked_evidence() -> None:
    result = _build(oos_rows=[])
    assert result["state"] == CANDIDATE_STATE
    assert result["evidence_state"] == BLOCKED_EVIDENCE
    assert any(str(item).startswith("window_") for item in result["blockers"])
    assert any(str(item).startswith("cross_season_missing") for item in result["blockers"])
    assert "scope_coverage_missing" in result["blockers"]


def test_entirely_missing_window_mapping_is_blocked_not_silently_defaulted() -> None:
    result = _build(windows=None)
    assert result["evidence_state"] == BLOCKED_EVIDENCE
    assert all(f"window_missing:{days}" in result["blockers"] for days in (3, 7, 15, 30))


def test_missing_row_scope_fields_fail_closed_without_key_error() -> None:
    rows = _rows()
    rows[0].pop("run_cycle")
    result = _build(oos_rows=rows)
    assert any(str(item).startswith("scope_binding_missing:") for item in result["blockers"])
    assert result["evidence_state"] == BLOCKED_EVIDENCE


def test_window_end_is_exclusive() -> None:
    rows = _rows()
    rows[0]["decision_at_utc"] = "2026-01-04T00:00:00Z"
    result = _build(oos_rows=rows)
    assert any(str(item).startswith("window_boundary_violation:3") for item in result["blockers"])
    assert result["evidence_state"] == BLOCKED_EVIDENCE


def test_feature_time_leakage_blocks_evidence() -> None:
    rows = _rows()
    rows[0]["feature_as_of_utc"] = "2026-01-01T02:00:00Z"
    result = _build(oos_rows=rows)
    assert any(str(item).startswith("time_leakage:") for item in result["blockers"])
    assert result["strict_asof_oos_metrics"]["strict_asof"] is False
    assert result["evidence_state"] == BLOCKED_EVIDENCE


def test_low_high_low_version_replay_is_blocked() -> None:
    with pytest.raises(ContractError, match="low_high_low"):
        validate_version_sequence(["P0", "P1", "P0"])
    result = _build(version_history=[1, 3, 2])
    assert "promotion_version_low_high_low" in result["blockers"]
    assert result["evidence_state"] == BLOCKED_EVIDENCE


def test_drift_is_blocked_even_when_windows_are_complete() -> None:
    result = _build(candidate_parameters={"bias": 1.0, "scale": 1.0})
    assert "drift_detected" in result["blockers"]
    assert result["evidence_state"] == BLOCKED_EVIDENCE


def test_candidate_hash_tampering_is_rejected() -> None:
    result = _build()
    tampered = copy.deepcopy(result)
    tampered["blockers"] = ["tampered"]
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        validate_probability_promotion_candidate(tampered)

    tampered = copy.deepcopy(result)
    tampered["candidate_artifact"]["parameters"]["bias"] = 0.2
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        validate_probability_promotion_candidate(tampered)


def test_parent_snapshot_and_m311_tampering_are_rejected() -> None:
    result = _build()
    parent = _parent()
    parent["parameters"]["bias"] = 0.2  # type: ignore[index]
    with pytest.raises(ContractError, match="parent_snapshot_mismatch"):
        validate_probability_promotion_candidate(result, parent_artifact=parent)

    tampered = copy.deepcopy(result)
    tampered["m3_11_equivalence_receipt"]["state"] = "NOT_EQUIVALENT"
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        validate_probability_promotion_candidate(tampered)


def test_external_candidate_parent_and_scope_binding_are_required() -> None:
    result = _build()
    candidate = copy.deepcopy(result["candidate_artifact"])
    candidate["parent_artifact_sha256"] = "c" * 64
    candidate["content_sha256"] = content_sha256({key: value for key, value in candidate.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        validate_probability_promotion_candidate({**result, "candidate_artifact": candidate})


def test_script_payload_path_uses_same_contract(tmp_path: Path) -> None:
    payload = {
        "parent_artifact": _parent(),
        "scope": SCOPE,
        "m3_11_equivalence_receipt": _equivalence(),
        "windows": _windows(),
        "as_of_utc": AS_OF,
        "oos_rows": _rows(),
        "candidate_parameters": {"bias": 0.1, "scale": 1.0},
        "cross_season_evidence": {"season_bins": ["spring", "summer", "autumn", "winter"]},
        "drift_evidence": {"detected": False, "max_parameter_delta": 0.0},
    }
    result = build_from_payload(payload)
    assert result["state"] == CANDIDATE_STATE
    assert result["review_boundary"]["calls_order_gateway"] is False

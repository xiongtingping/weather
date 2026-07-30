from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bayes_basket_target.execution_version_vector import (
    DELTA_V2_ECONOMIC_POLICY_SCHEMA,
    LEGACY_SCHEMA,
    NO_EXECUTION_POLICY_SCHEMA,
    NO_EXECUTION_VECTOR_POLICY,
    SCHEMA,
    ExecutionVersionVectorError,
    build_no_execution_version_policy,
    build_execution_version_vector,
    decode_legacy_execution_version_vector,
    execution_version_vector_sha256,
    validate_execution_version_vector,
)


SHA_FIELDS = (
    "probability_sha256",
    "official_ws_quote_sha256",
    "inventory_sha256",
    "order_state_sha256",
    "station_policy_sha256",
    "cash_policy_decision_sha256",
    "economic_policy_artifact_sha256",
)


def _raw() -> dict[str, str]:
    return {
        "schema": SCHEMA,
        **{
            field: digit * 64
            for field, digit in zip(SHA_FIELDS, ("a", "b", "c", "d", "e", "f", "0"))
        },
        "economic_policy_schema": DELTA_V2_ECONOMIC_POLICY_SCHEMA,
        "target_price_id": "target_price_RKPK_2026-07-27_36",
        "quote_observed_at": "2026-07-27T04:00:00+09:00",
        "quote_expires_at": "2026-07-27T04:01:00+09:00",
    }


def test_builds_immutable_vector_with_all_required_lineage() -> None:
    vector = build_execution_version_vector(_raw())

    assert vector.schema == SCHEMA
    assert vector["quote_observed_at"] == "2026-07-26T19:00:00Z"
    assert vector["quote_expires_at"] == "2026-07-26T19:01:00Z"
    assert len(vector.vector_sha256) == 64
    assert vector.to_dict()["economic_policy_artifact_sha256"] == "0" * 64
    assert vector.to_dict()["economic_policy_schema"] == (
        DELTA_V2_ECONOMIC_POLICY_SCHEMA
    )
    with pytest.raises(FrozenInstanceError):
        vector.target_price_id = "changed"  # type: ignore[misc]


def test_digest_is_deterministic_and_order_independent() -> None:
    raw = _raw()
    reordered = {key: raw[key] for key in reversed(tuple(raw))}

    first = validate_execution_version_vector(raw)
    second = validate_execution_version_vector(reordered)
    assert first == second
    assert first.vector_sha256 == execution_version_vector_sha256(raw)

    changed = {**raw, "inventory_sha256": "0" * 64}
    assert validate_execution_version_vector(changed).vector_sha256 != first.vector_sha256


@pytest.mark.parametrize("field", SHA_FIELDS + ("target_price_id", "quote_observed_at", "quote_expires_at"))
def test_missing_or_unknown_required_field_fails_closed(field: str) -> None:
    raw = _raw()
    raw.pop(field)
    with pytest.raises(ExecutionVersionVectorError):
        validate_execution_version_vector(raw)

    raw = _raw()
    raw[field] = "unknown"
    with pytest.raises(ExecutionVersionVectorError):
        validate_execution_version_vector(raw)


@pytest.mark.parametrize(
    ("observed", "expires", "message"),
    [
        ("2026-07-27T04:00:00Z", "2026-07-27T04:00:00Z", "must_be_after"),
        ("2026-07-27T04:01:00Z", "2026-07-27T04:00:00Z", "must_be_after"),
        ("2026-07-27T04:00:00", "2026-07-27T04:01:00Z", "timezone_required"),
        ("2026-07-27T04:00:00Z", "2026-07-27T04:01:00", "timezone_required"),
    ],
)
def test_quote_clock_is_aware_and_expiry_is_strictly_after(
    observed: str, expires: str, message: str
) -> None:
    raw = {**_raw(), "quote_observed_at": observed, "quote_expires_at": expires}
    with pytest.raises(ExecutionVersionVectorError, match=message):
        validate_execution_version_vector(raw)


def test_supplied_digest_and_unknown_extra_field_fail_closed() -> None:
    raw = _raw()
    valid = build_execution_version_vector(raw)
    assert build_execution_version_vector({**raw, "vector_sha256": valid.vector_sha256}) == valid

    with pytest.raises(ExecutionVersionVectorError, match="content_address_mismatch"):
        validate_execution_version_vector({**raw, "vector_sha256": "f" * 64})
    with pytest.raises(ExecutionVersionVectorError, match="unknown_fields:future_field"):
        validate_execution_version_vector({**raw, "future_field": "must-not-be-ignored"})
    with pytest.raises(ExecutionVersionVectorError, match="unknown_fields:1"):
        validate_execution_version_vector({**raw, 1: "must-not-be-ignored"})  # type: ignore[dict-item]


def test_digest_inputs_are_canonicalized_without_accepting_unknowns() -> None:
    raw = _raw()
    upper = {**raw, "probability_sha256": raw["probability_sha256"].upper()}
    assert validate_execution_version_vector(upper) == validate_execution_version_vector(raw)

    equivalent_clock = {
        **raw,
        "quote_observed_at": "2026-07-26T19:00:00Z",
        "quote_expires_at": "2026-07-26T19:01:00Z",
    }
    assert validate_execution_version_vector(equivalent_clock) == validate_execution_version_vector(raw)


def test_no_database_or_service_side_effects_are_needed() -> None:
    vector = build_execution_version_vector(_raw())
    assert vector["probability_sha256"] == "a" * 64
    assert vector["official_ws_quote_sha256"] == "b" * 64
    assert vector["cash_policy_decision_sha256"] == "f" * 64


def test_v1_is_explicit_historical_decode_only() -> None:
    raw = _raw()
    raw.pop("economic_policy_schema")
    raw["schema"] = LEGACY_SCHEMA
    raw["strict_delta_artifact_sha256"] = raw.pop(
        "economic_policy_artifact_sha256"
    )
    legacy = decode_legacy_execution_version_vector(raw)
    assert legacy.schema == LEGACY_SCHEMA
    assert legacy["strict_delta_artifact_sha256"] == "0" * 64
    with pytest.raises(ExecutionVersionVectorError):
        build_execution_version_vector(raw)


def test_no_execution_policy_has_no_fake_price_or_quote_clock() -> None:
    policy = build_no_execution_version_policy(
        {"policy": NO_EXECUTION_VECTOR_POLICY, "reason_codes": ["missing_quote"]}
    )
    assert policy.schema == NO_EXECUTION_POLICY_SCHEMA
    assert policy.to_dict()["reason_codes"] == ["missing_quote"]
    assert "target_price_id" not in policy.to_dict()
    assert "quote_expires_at" not in policy.to_dict()


def test_no_execution_policy_drift_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(ExecutionVersionVectorError, match="content_address_mismatch"):
        build_no_execution_version_policy(
            {
                "policy": NO_EXECUTION_VECTOR_POLICY,
                "reason_codes": ["missing_quote"],
                "policy_sha256": "f" * 64,
            }
        )
    with pytest.raises(ExecutionVersionVectorError, match="unknown_fields"):
        build_no_execution_version_policy(
            {
                "policy": NO_EXECUTION_VECTOR_POLICY,
                "reason_codes": ["missing_quote"],
                "target_price_id": "must-not-exist",
            }
        )

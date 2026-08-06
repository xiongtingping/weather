from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.canonical_contract import (
    CanonicalTypedScopeEnvelope,
    TYPED_SCOPE_ENVELOPE_SCHEMA,
)
from bayes_basket_target.contracts import ContractError, content_sha256


AS_OF = "2026-08-04T00:00:00Z"


VALID_KEYS = {
    ("TRUTH_OBSERVATION", "REVISION"): {
        "station_id": "KATL", "event_date": "2026-07-20",
        "settlement_timezone": "America/New_York", "observation_id": "rtobs-1",
        "revision_no": 1, "observed_at_utc": "2026-07-20T12:00:00Z",
        "canonical_committed_at_utc": "2026-07-20T12:01:00Z", "source_lane": "AWC",
    },
    ("TRUTH_DAY", "DAY_BOUNDS"): {
        "station_id": "KATL", "event_date": "2026-07-20",
        "settlement_timezone": "America/New_York", "bounds_version": 1,
    },
    ("TAF_P0M", "ARTIFACT"): {
        "station_id": "KATL", "settlement_timezone": "America/New_York",
        "season": "JJA", "lead_bucket": "d1", "run_cycle": "PREVIOUS_DAY1",
    },
    ("TAF_P0M", "EVENT"): {
        "station_id": "KATL", "event_date": "2026-07-20",
        "decision_at_utc": "2026-07-19T12:00:00Z", "p0_root_sha256": "a" * 64,
        "taf_revision_id": "taf-katl-20260719-r1",
    },
    ("TRAJECTORY_P1", "ARTIFACT"): {
        "station_id": "KATL", "source_lane": "AWC", "season": "JJA",
        "intraday_bucket": "MORNING", "maturity": "D0",
        "settlement_definition": "daily_high_et_v1",
    },
    ("TRAJECTORY_P1", "EVENT"): {
        "station_id": "KATL", "event_date": "2026-07-20",
        "decision_at_utc": "2026-07-20T12:00:00Z",
        "probability_root_sha256": "b" * 64, "observation_id": "awc-katl-1",
    },
    ("STRATEGY_UTILITY", "EVALUATION"): {
        "station_id": "KATL", "event_date": "2026-07-20",
        "decision_at_utc": "2026-07-20T12:00:00Z",
        "settlement_timezone": "America/New_York", "policy_stage": "P1",
        "strategy_version": "pure_yes_v1", "season": "JJA", "maturity": "D0",
        "liquidity_regime": "STANDARD", "candidate_inventory_sha256": "c" * 64,
        "baseline_inventory_sha256": "d" * 64, "ws_snapshot_sha256": "e" * 64,
    },
    ("MARKET_WS", "REGISTRY"): {
        "station_id": "KATL", "event_date": "2026-07-20", "market_id": "market-1",
        "yes_token_id": "yes-1", "bucket_contract_sha256": "f" * 64,
    },
    ("MARKET_WS", "SNAPSHOT"): {
        "station_id": "KATL", "event_date": "2026-07-20", "market_id": "market-1",
        "yes_token_id": "yes-1", "ws_sequence": 42,
        "snapshot_at_utc": "2026-07-20T12:00:00Z",
    },
    ("RUNTIME", "SERVICE_RELEASE"): {
        "service_label": "com.weather.weather_basket_yes", "launch_domain": "gui/501",
        "runtime_release_id": "runtime-release-1",
    },
}


def _record(scope_type: str, scope_kind: str, *, state: str = "BLOCKED") -> dict:
    identity = {
        "scope_type": scope_type,
        "scope_kind": scope_kind,
        "natural_key": deepcopy(VALID_KEYS[(scope_type, scope_kind)]),
        "version": None,
        "as_of_utc": AS_OF,
        "state": state,
        "blockers": [] if state.endswith("READY") else ["ACTIVATION_ROOT_MISSING"],
        "evidence_content_sha256": "1" * 64,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _envelope(scope_type: str, records: list[dict]) -> dict:
    identity = {
        "schema": TYPED_SCOPE_ENVELOPE_SCHEMA,
        "as_of_utc": AS_OF,
        "builder_id": f"{scope_type.lower()}.builder.v1",
        "scope_type": scope_type,
        "records": records,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


@pytest.mark.parametrize("scope_type,scope_kind", sorted(VALID_KEYS))
def test_each_typed_scope_natural_key_is_exact(
    scope_type: str, scope_kind: str
) -> None:
    raw = _envelope(scope_type, [_record(scope_type, scope_kind)])
    parsed = CanonicalTypedScopeEnvelope.from_mapping(raw)
    assert parsed.to_mapping() == raw
    assert parsed.records[0].scope_key


def test_typed_scope_rejects_missing_or_guessed_natural_key_field() -> None:
    record = _record("TRUTH_DAY", "DAY_BOUNDS")
    record["natural_key"].pop("bounds_version")
    record["content_sha256"] = content_sha256(
        {key: value for key, value in record.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="canonical_typed_scope_natural_key_fields_invalid"
    ):
        CanonicalTypedScopeEnvelope.from_mapping(_envelope("TRUTH_DAY", [record]))


def test_zero_scope_placeholder_and_future_oos_are_forbidden() -> None:
    with pytest.raises(
        ContractError, match="canonical_typed_scope_envelope_records_required"
    ):
        CanonicalTypedScopeEnvelope.from_mapping(_envelope("RUNTIME", []))
    raw = _envelope("RUNTIME", [_record("RUNTIME", "SERVICE_RELEASE")])
    raw["future_data_counts_as_oos"] = True
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="canonical_typed_scope_envelope_future_oos_forbidden"
    ):
        CanonicalTypedScopeEnvelope.from_mapping(raw)


def test_duplicate_scope_and_ready_with_blocker_fail_closed() -> None:
    record = _record("MARKET_WS", "SNAPSHOT")
    with pytest.raises(ContractError, match="canonical_typed_scope_fact_scope_duplicate"):
        CanonicalTypedScopeEnvelope.from_mapping(
            _envelope("MARKET_WS", [record, deepcopy(record)])
        )
    invalid = _record("MARKET_WS", "SNAPSHOT", state="DATA_READY")
    invalid["blockers"] = ["DATA_MISSING"]
    invalid["content_sha256"] = content_sha256(
        {key: value for key, value in invalid.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="canonical_typed_scope_state_blocker_mismatch"
    ):
        CanonicalTypedScopeEnvelope.from_mapping(_envelope("MARKET_WS", [invalid]))


def test_runtime_system_domain_and_non_awc_trajectory_are_rejected() -> None:
    runtime = _record("RUNTIME", "SERVICE_RELEASE")
    runtime["natural_key"]["launch_domain"] = "system"
    runtime["content_sha256"] = content_sha256(
        {key: value for key, value in runtime.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="canonical_typed_scope_launch_domain_invalid"):
        CanonicalTypedScopeEnvelope.from_mapping(_envelope("RUNTIME", [runtime]))
    trajectory = _record("TRAJECTORY_P1", "ARTIFACT")
    trajectory["natural_key"]["source_lane"] = "AMSC"
    trajectory["content_sha256"] = content_sha256(
        {key: value for key, value in trajectory.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="canonical_typed_scope_source_lane_invalid"):
        CanonicalTypedScopeEnvelope.from_mapping(
            _envelope("TRAJECTORY_P1", [trajectory])
        )

from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.market_materialization import build_official_ws_snapshot
from bayes_basket_target.quote_freshness import (
    SCHEMA,
    QuoteFreshnessPolicy,
)


VALID_FROM = "2026-07-01T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"
OBSERVED = "2026-07-23T05:00:00Z"
CANDIDATE_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


def _policy(
    *,
    max_age: object = 5.0,
    authority_scope: str = CANDIDATE_SCOPE,
    valid_from: str = VALID_FROM,
    valid_until: str = VALID_UNTIL,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": SCHEMA,
        "policy_id": "quote-freshness-adversarial-test-v1",
        "max_quote_age_seconds": max_age,
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "authority_scope": authority_scope,
        "evidence_status": "DESIGN_CANDIDATE",
    }
    value["content_sha256"] = content_sha256(value)
    return value


def test_policy_digest_mismatch_fails_closed() -> None:
    raw = _policy()
    raw["max_quote_age_seconds"] = 6.0

    with pytest.raises(
        ContractError,
        match="quote_freshness_policy_content_address_mismatch",
    ):
        QuoteFreshnessPolicy.from_mapping(raw)


def test_required_authority_scope_mismatch_fails_closed() -> None:
    policy = QuoteFreshnessPolicy.from_mapping(_policy())

    with pytest.raises(
        ContractError,
        match="quote_freshness_authority_scope_mismatch",
    ):
        policy.derive_expiry(
            quote_observed_at=OBSERVED,
            required_authority_scope="FORMAL_PROMOTED",
        )


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-06-30T23:59:59Z",
        VALID_UNTIL,
        "2026-08-01T00:00:00.000001Z",
    ],
)
def test_policy_inactive_at_or_outside_validity_window_fails_closed(
    observed_at: str,
) -> None:
    policy = QuoteFreshnessPolicy.from_mapping(_policy())

    with pytest.raises(ContractError, match="quote_freshness_policy_not_active"):
        policy.derive_expiry(
            quote_observed_at=observed_at,
            required_authority_scope=CANDIDATE_SCOPE,
        )


def test_validity_window_lower_boundary_is_inclusive() -> None:
    policy = QuoteFreshnessPolicy.from_mapping(_policy())

    assert policy.derive_expiry(
        quote_observed_at=VALID_FROM,
        required_authority_scope=CANDIDATE_SCOPE,
    ) == "2026-07-01T00:00:05Z"


@pytest.mark.parametrize("max_age", [0, -0.000001, 300.000001])
def test_max_quote_age_outside_positive_hard_limit_fails_closed(
    max_age: float,
) -> None:
    with pytest.raises(ContractError, match="quote_freshness_max_age_invalid"):
        QuoteFreshnessPolicy.from_mapping(_policy(max_age=max_age))


def test_max_quote_age_hard_limit_is_inclusive() -> None:
    policy = QuoteFreshnessPolicy.from_mapping(_policy(max_age=300.0))

    assert policy.derive_expiry(
        quote_observed_at=OBSERVED,
        required_authority_scope=CANDIDATE_SCOPE,
    ) == "2026-07-23T05:05:00Z"


def test_exact_five_second_expiry_normalizes_observation_to_utc() -> None:
    policy = QuoteFreshnessPolicy.from_mapping(_policy(max_age=5.0))

    assert policy.derive_expiry(
        quote_observed_at="2026-07-23T13:00:00+08:00",
        required_authority_scope=CANDIDATE_SCOPE,
    ) == "2026-07-23T05:00:05Z"


def _official_snapshot(*, quote_expires_at: str) -> dict[str, object]:
    observed_at = OBSERVED
    return {
        "snapshot_id": "ws_quote-freshness-boundary",
        "market_id": "basket_quote-freshness-boundary",
        "station_id": "RKPK",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Seoul",
        "sequence": 1,
        "observed_at": observed_at,
        "quote_version": "quote-version-v1",
        "quote_observed_at": observed_at,
        "quote_expires_at": quote_expires_at,
        "quote_freshness_policy_sha256": "a" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-state-v1",
        "remaining_budget_usdc": 10.0,
        "buckets": [
            {
                "bucket": "35°C",
                "ordinal": 0,
                "ask_levels": [{"price": 0.4, "shares": 1.0}],
            }
        ],
        "cash_phase_input": {
            "policy_version": "cash-v1",
            "station_id": "RKPK",
            "current_phase": "HEAVY",
            "event_date_local": "2026-07-23",
            "observed_at_utc": observed_at,
            "station_timezone": "Asia/Seoul",
            "checkpoint_id": "checkpoint-v1",
        },
        "state": "COMMITTED",
        "created_at": observed_at,
    }


def test_snapshot_expiry_equal_to_observation_is_expired_and_rejected() -> None:
    with pytest.raises(
        ContractError,
        match="official_ws_quote_expiry_must_follow_observation",
    ):
        build_official_ws_snapshot(_official_snapshot(quote_expires_at=OBSERVED))


def test_snapshot_expiry_one_microsecond_after_observation_is_valid() -> None:
    row = build_official_ws_snapshot(
        _official_snapshot(quote_expires_at="2026-07-23T05:00:00.000001Z")
    )

    assert row["quote_expires_at"] == "2026-07-23T05:00:00.000001Z"


def test_policy_mapping_is_not_mutated_while_digest_is_verified() -> None:
    raw = _policy()
    original = deepcopy(raw)

    policy = QuoteFreshnessPolicy.from_mapping(raw)

    assert raw == original
    assert policy.content_sha256 == original["content_sha256"]

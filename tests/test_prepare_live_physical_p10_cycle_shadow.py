from __future__ import annotations

from scripts.prepare_live_physical_p10_cycle_shadow import (
    _event_valid_until,
    _realization_policy,
)


def test_event_dependency_expiry_covers_the_full_zuuu_local_day() -> None:
    assert _event_valid_until("2026-07-14") == "2026-07-14T15:59:59Z"


def test_physical_only_realization_policy_is_content_addressed_and_not_market_input() -> None:
    policy = _realization_policy()

    assert policy["schema"] == "realization_state_policy_v2"
    assert policy["approval_state"] == "approved"
    assert policy["scope"] == "physical_posterior_cycle_market_projection_na"
    assert len(policy["artifact_sha256"]) == 64

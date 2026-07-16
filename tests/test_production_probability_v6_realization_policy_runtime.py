import json
from pathlib import Path

import pytest

from production_probability_v6_realization_policy_runtime import (
    artifact_sha256,
    load_v6_realization_policy,
    validate_v6_realization_policy,
)


ROOT = Path(__file__).resolve().parents[1]


def test_v6_realization_policy_is_content_addressed_and_has_no_trade_authority():
    policy = load_v6_realization_policy()

    assert policy["artifact_sha256"] == artifact_sha256(policy)
    assert policy["scope"] == "production_v6_market_projection_only"
    assert policy["ev_edge_or_market_price_consumed"] is False
    assert policy["places_orders"] is False
    assert policy["authorizes_clob"] is False


def test_v6_realization_policy_rejects_threshold_mutation():
    policy = json.loads(
        (ROOT / "config/trading/production_probability_v6_realization_policy.json").read_text()
    )
    policy["strong_exclusion_probability_ceiling"] = 0.2

    with pytest.raises(ValueError, match="artifact_sha"):
        validate_v6_realization_policy(policy)

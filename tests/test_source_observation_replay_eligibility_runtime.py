from __future__ import annotations

from source_observation_replay_eligibility_runtime import (
    classify_source_observation_replay_eligibility,
)


def test_posthoc_fact_feature_adapter_is_excluded_without_mutating_or_retiming_it() -> None:
    legacy = classify_source_observation_replay_eligibility(
        {"parser_version": "posterior_ssot_projection_fact_feature_source_adapter_v1"}
    )
    current = classify_source_observation_replay_eligibility({"parser_version": "forward-capture-v2"})

    assert legacy["state"] == "excluded_legacy_derived"
    assert legacy["strict_source_lineage_candidate"] is False
    assert "posthoc" in legacy["reason"]
    assert current["state"] == "candidate"
    assert current["strict_source_lineage_candidate"] is True

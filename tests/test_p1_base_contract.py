from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.p1_base_contract import (
    CONTRACT_SCHEMA,
    PROJECTION_METHOD,
    CanonicalP1BaseContract,
    CanonicalP1BaseFact,
)


def _sha(char: str) -> str:
    return char * 64


def _fact(**overrides: object) -> dict[str, object]:
    probabilities = [
        {"ordinal": 0, "outcome": "25C", "probability": "0.000000000000"},
        {"ordinal": 1, "outcome": "26C", "probability": "0.650000000000"},
        {"ordinal": 2, "outcome": "27C", "probability": "0.350000000000"},
    ]
    identity: dict[str, object] = {
        "station_id": "KATL",
        "event_date": "2026-08-05",
        "settlement_timezone": "America/New_York",
        "p0_forecast_snapshot_content_sha256": _sha("a"),
        "market_bucket_bridge_content_sha256": _sha("c"),
        "parent_bucket_probability_vector_sha256": _sha("d"),
        "surface_observation_winner_content_sha256": _sha("b"),
        "p1_oos_artifact_content_sha256": None,
        "surface_observation_source_lane": "AWC",
        "surface_observation_source_class": "official_awc",
        "surface_observation_report_type": "SPECI",
        "surface_observation_valid_at_utc": "2026-08-05T14:01:00.000Z",
        "surface_observation_committed_at_utc": "2026-08-05T14:02:00.000Z",
        "decision_at_utc": "2026-08-05T14:02:00.000Z",
        "freshness_checked_at_utc": "2026-08-05T14:02:00.000Z",
        "freshness_expires_at_utc": "2026-08-05T14:07:00.000Z",
        "reported_high_c": "26.000000000000",
        "hard_lower_bound_c": "25.950000000000",
        "temperature_resolution_c": "0.100000000000",
        "hard_lower_bound_method": "ROUND_NEAREST_RESOLUTION_LOWER_EDGE",
        "hard_excluded_ordinals": [0],
        "bucket_probabilities": probabilities,
        "bucket_probability_vector_sha256": content_sha256(probabilities),
        "probability_projection_method": PROJECTION_METHOD,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    identity.update(overrides)
    identity["content_sha256"] = content_sha256(identity)
    return identity


def _contract(*facts: dict[str, object]) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": "2026-08-05T14:03:00.000Z",
        "builder_id": "canonical.p1_base.v1",
        "p1_base_facts": list(facts),
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_p1_base_fact_binds_parents_clocks_and_ordinal_probabilities() -> None:
    parsed = CanonicalP1BaseFact.from_mapping(_fact())

    assert parsed.raw["station_id"] == "KATL"
    assert parsed.raw["reported_high_c"] == "26.000000000000"
    assert parsed.raw["bucket_probabilities"][0]["ordinal"] == 0
    assert parsed.scope_key.split("|")[0:3] == ["KATL", "2026-08-05", "America/New_York"]
    assert CanonicalP1BaseContract.from_mapping(_contract(parsed.raw)).content_sha256


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda value: value.pop("surface_observation_source_lane"),
            "p1_base_fact_fields_mismatch",
        ),
        (
            lambda value: value.update({
                "surface_observation_source_lane": "MADIS",
                "surface_observation_source_class": "official_awc",
            }),
            "p1_base_surface_source_lineage_invalid",
        ),
        (
            lambda value: value.update({
                "surface_observation_source_lane": "MADIS",
                "surface_observation_source_class": "official_republisher",
                "surface_observation_report_type": "ASOS_HF",
            }),
            "p1_base_asos_oos_artifact_required",
        ),
        (
            lambda value: value.update({"hard_excluded_ordinals": [1]}),
            "p1_base_hard_exclusion_probability_mismatch",
        ),
        (
            lambda value: value.update({"freshness_expires_at_utc": "2026-08-05T14:02:00Z"}),
            "p1_base_clock_order_invalid",
        ),
    ],
)
def test_p1_base_rejects_source_relabeling_and_invalid_lineage(
    mutate: object, error: str,
) -> None:
    raw = _fact()
    mutate(raw)  # type: ignore[operator]
    # The field-set failure happens before content-address verification;
    # all semantic cases must reseal so that their targeted gate is reached.
    if "content_sha256" in raw:
        raw["content_sha256"] = content_sha256({
            key: value for key, value in raw.items() if key != "content_sha256"
        })
    with pytest.raises(ContractError, match=error):
        CanonicalP1BaseFact.from_mapping(raw)


def test_p1_base_rejects_probability_vector_hash_mismatch() -> None:
    raw = _fact(bucket_probability_vector_sha256=_sha("f"))
    with pytest.raises(ContractError, match="p1_base_bucket_probability_hash_mismatch"):
        CanonicalP1BaseFact.from_mapping(raw)


def test_p1_base_contract_rejects_duplicate_exact_scope() -> None:
    first = _fact()
    second = deepcopy(first)
    second["content_sha256"] = content_sha256({
        key: value for key, value in second.items() if key != "content_sha256"
    })
    with pytest.raises(ContractError, match="p1_base_scope_duplicate"):
        CanonicalP1BaseContract.from_mapping(_contract(first, second))

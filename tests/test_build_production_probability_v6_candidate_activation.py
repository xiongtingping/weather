from __future__ import annotations

from scripts.build_production_probability_v6_candidate_activation import build


def test_repository_candidate_activation_is_content_bound() -> None:
    value = build(__import__("pathlib").Path(__file__).resolve().parents[1])
    assert value["schema"] == "production_probability_v6_activation_v1"
    assert value["activation_requires_atomic_install"] is True
    assert value["supersedes_probability_epoch_schema_on_cutover"] == (
        "production_probability_v6_activation_v1"
    )
    assert len(value["activation_contract_sha256"]) == 64
    assert value["v5_fallback_forbidden"] is True
    assert value["places_orders"] is False
    assert value["authorizes_clob"] is False

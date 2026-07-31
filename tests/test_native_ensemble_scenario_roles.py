from __future__ import annotations

import copy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_scenario_roles import (
    SCHEMA,
    select_native_ensemble_scenario_roles,
)


BUCKET_ORDINALS = {
    "23°C": 0,
    "24°C": 1,
    "25°C": 2,
    "26°C": 3,
    "27°C": 4,
    "28°C": 5,
}


def _product(
    product_id: str,
    family_id: str,
    probabilities: dict[str, float],
    *,
    reliability: float = 1.0,
) -> dict[str, object]:
    return {
        "ensemble_system_id": product_id,
        "independence_family_id": family_id,
        "raw_bucket_probabilities": probabilities,
        "oos_reliability": reliability,
        "member_set_sha256": (product_id.lower() * 64)[:64],
        "lineage": {
            "source_archive_sha256": (family_id.lower() * 64)[:64],
            "product": product_id,
        },
    }


def _august_first_products() -> list[dict[str, object]]:
    # The roles intentionally mirror the requested example: ECMWF IFS has a
    # warm mode at 28°C and a 26°C runner-up; AIFS has 26°C as its mode;
    # ICON has a cold mode at 25°C and 26°C as runner-up; GEM is a deeper cold
    # competitor whose mode is 24°C.
    return [
        _product(
            "ECMWF_IFS",
            "ECMWF",
            {
                "23°C": 0.02,
                "24°C": 0.101,
                "25°C": 0.15,
                "26°C": 0.255,
                "27°C": 0.18,
                "28°C": 0.294,
            },
        ),
        _product(
            "ECMWF_AIFS",
            "ECMWF",
            {
                "23°C": 0.01,
                "24°C": 0.092,
                "25°C": 0.20,
                "26°C": 0.314,
                "27°C": 0.294,
                "28°C": 0.09,
            },
        ),
        _product(
            "ICON_GLOBAL",
            "DWD_ICON_EPS",
            {
                "23°C": 0.00,
                "24°C": 0.05,
                "25°C": 0.525,
                "26°C": 0.275,
                "27°C": 0.10,
                "28°C": 0.05,
            },
        ),
        _product(
            "ECCC_GEM",
            "ECCC_GEM",
            {
                "23°C": 0.238,
                "24°C": 0.381,
                "25°C": 0.20,
                "26°C": 0.048,
                "27°C": 0.08,
                "28°C": 0.053,
            },
        ),
    ]


def test_three_roles_follow_family_deduplicated_consensus_and_mode_rules():
    result = select_native_ensemble_scenario_roles(
        _august_first_products(),
        BUCKET_ORDINALS,
        station_id="ZUUU",
        event_date="2026-08-01",
        p1_center_bucket="26°C",
        p0m_center_bucket="26°C",
    )

    assert result["schema"] == SCHEMA
    assert result["buckets"] == {"L": "25°C", "M": "26°C", "H": "28°C"}
    assert result["roles"]["M"]["score"]["independent_family_count"] == 2
    assert result["roles"]["M"]["supporting_families"] == [
        "DWD_ICON_EPS",
        "ECMWF",
    ]
    assert result["roles"]["H"]["source_product"]["ensemble_system_id"] == (
        "ECMWF_IFS"
    )
    assert result["roles"]["L"]["source_product"]["ensemble_system_id"] == (
        "DWD_ICON_EPS"
    ) or result["roles"]["L"]["source_product"]["ensemble_system_id"] == (
        "ICON_GLOBAL"
    )
    assert result["roles"]["H"]["score"]["distance_layers"] == 2
    assert result["roles"]["L"]["score"]["distance_layers"] == 1

    observations = result["observations_by_side"]["deep_cold"]
    assert any(
        item["bucket"] == "24°C"
        and item["source_product"]["ensemble_system_id"] == "ECCC_GEM"
        for item in observations
    )
    assert all("lineage" in item for item in observations)
    assert result["raw_member_probability_is_not_p0_position"] is True
    assert result["orders_submitted"] == 0
    assert result["live_submit_authorized"] is False


def test_products_in_one_family_do_not_create_extra_consensus_votes():
    products = [
        _product("ECMWF_IFS", "ECMWF", {"25°C": 0.6, "26°C": 0.4}),
        _product("ECMWF_AIFS", "ECMWF", {"25°C": 0.7, "26°C": 0.3}),
        _product("ICON_GLOBAL", "DWD_ICON_EPS", {"25°C": 0.55, "26°C": 0.45}),
        _product("GEM", "ECCC_GEM", {"24°C": 0.7, "25°C": 0.3}),
    ]
    result = select_native_ensemble_scenario_roles(
        products,
        {"24°C": 0, "25°C": 1, "26°C": 2},
    )

    # 25°C is Top-2 for all products, but it receives three family votes,
    # never four product votes.  The M evidence must expose one ECMWF vote.
    assert result["buckets"]["M"] == "25°C"
    m_sources = result["roles"]["M"]["supporting_products"]
    assert len(m_sources) == 3
    assert [row["independence_family_id"] for row in m_sources].count("ECMWF") == 1
    assert result["roles"]["M"]["score"]["independent_family_count"] == 3


def test_tail_requires_other_family_adjacent_nonzero_support_and_keeps_observation():
    products = [
        _product("ECMWF_IFS", "ECMWF", {"25°C": 0.8, "26°C": 0.2}),
        _product("ICON_GLOBAL", "DWD_ICON_EPS", {"25°C": 0.6, "26°C": 0.4}),
        # GEM's mode is two layers cold, but it has no probability at 24/25,
        # so it cannot be a tail role without adjacent cross-family support.
        _product("GEM", "ECCC_GEM", {"23°C": 1.0, "25°C": 0.0}),
    ]
    result = select_native_ensemble_scenario_roles(
        products,
        {"23°C": 0, "24°C": 1, "25°C": 2, "26°C": 3},
    )

    assert result["buckets"]["M"] == "25°C"
    assert result["buckets"]["H"] is None
    assert result["buckets"]["L"] is None
    assert any(
        item["bucket"] == "23°C"
        and "cross_family_adjacent_support_missing" in item["reasons"]
        for item in result["observation_candidates"]
    )


def test_no_consensus_leaves_roles_empty_and_does_not_force_a_bucket():
    products = [
        _product("ECMWF_IFS", "ECMWF", {"25°C": 0.9, "26°C": 0.1}),
        _product("ICON_GLOBAL", "DWD_ICON_EPS", {"24°C": 0.9, "23°C": 0.1}),
    ]
    result = select_native_ensemble_scenario_roles(
        products,
        {"23°C": 0, "24°C": 1, "25°C": 2, "26°C": 3},
    )

    assert result["roles"] == {"L": None, "M": None, "H": None}
    assert result["buckets"] == {"L": None, "M": None, "H": None}
    assert result["blockers"] == [
        "no_bucket_has_two_independent_family_top2_support"
    ]


def test_result_is_deterministic_content_addressed_and_does_not_mutate_input():
    products = _august_first_products()
    original = copy.deepcopy(products)
    first = select_native_ensemble_scenario_roles(
        products,
        BUCKET_ORDINALS,
        p1_center_bucket="26°C",
    )
    second = select_native_ensemble_scenario_roles(
        list(reversed(products)),
        dict(reversed(list(BUCKET_ORDINALS.items()))),
        p1_center_bucket="26°C",
    )

    assert products == original
    assert first == second
    identity = {key: value for key, value in first.items() if key != "content_sha256"}
    assert first["content_sha256"] == content_sha256(identity)
    assert len(first["content_sha256"]) == 64


def test_raw_member_buckets_can_be_derived_from_existing_member_shape():
    products = [
        {
            "ensemble_system_id": "ECMWF_IFS",
            "independence_family_id": "ECMWF",
            "members": [
                {"member_id": "m1", "raw_tmax_c": 25.2},
                {"member_id": "m2", "raw_tmax_c": 25.7},
                {"member_id": "m3", "raw_tmax_c": 26.2},
            ],
        },
        {
            "ensemble_system_id": "ICON_GLOBAL",
            "independence_family_id": "DWD_ICON_EPS",
            "members": [
                {"member_id": "m1", "raw_tmax_c": 25.1},
                {"member_id": "m2", "raw_tmax_c": 25.4},
                {"member_id": "m3", "raw_tmax_c": 26.1},
            ],
        },
    ]
    result = select_native_ensemble_scenario_roles(
        products,
        bucket_intervals={
            "24°C": [None, 24.5],
            "25°C": [24.5, 25.5],
            "26°C": [25.5, None],
        },
    )

    assert result["buckets"]["M"] == "25°C"
    assert result["source_product_lineage"][0]["raw_bucket_probability_sha256"]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        (
            {
                "products": [
                    _product("A", "F", {"25°C": 1.0}),
                ],
                "bucket_ordinals": {"25°C": 0},
                "max_role_distance": 3,
            },
            "scenario_max_role_distance_invalid",
        ),
        (
            {
                "products": [
                    _product("A", "F", {"25°C": 1.0}),
                ],
                "bucket_ordinals": {"25°C": 0, "26°C": 0},
            },
            "scenario_bucket_ordinal_duplicate",
        ),
    ],
)
def test_invalid_contracts_fail_closed(kwargs, error):
    with pytest.raises(ContractError, match=error):
        select_native_ensemble_scenario_roles(**kwargs)

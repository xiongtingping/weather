from __future__ import annotations

import hashlib

from resolution_bridge_runtime import (
    build_final_resolution_label,
    build_resolution_bridge,
    validate_final_resolution_label,
)


def _contract(**overrides):
    contract = {
        "resolution_contract_id": "resolution_zuuu_daily_high_v1",
        "resolution_source_id": "official_resolution_source",
        "resolution_local_date": "2026-07-03",
        "settlement_unit": "C",
        "physical_to_settlement_kernel": {
            "30": [{"settlement_value_c": 30.0, "probability": 1.0}],
            "31": [{"settlement_value_c": 31.0, "probability": 1.0}],
        },
        "bucket_contract": {
            "30°C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31°C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5},
            "32°C or higher": {"lower_inclusive": 31.5, "upper_exclusive": 99.0},
        },
        "bridge_closed": True,
        "validated": True,
    }
    contract.update(overrides)
    return contract


def test_explicit_kernel_maps_physical_distribution_to_settlement_and_market_bucket() -> None:
    result = build_resolution_bridge(
        physical_high_distribution=[
            {"temperature_c": 30.0, "probability": 0.4},
            {"temperature_c": 31.0, "probability": 0.6},
        ],
        resolution_contract=_contract(),
    )

    assert result["ok"] is True
    assert result["market_data_consumed"] is False
    assert result["market_bucket_probabilities"] == {
        "30°C": 0.4,
        "31°C": 0.6,
        "32°C or higher": 0.0,
    }
    assert result["settlement_exclusion_certificates"]["32°C or higher"]["state"] == "valid"


def test_missing_kernel_or_bucket_coverage_fails_closed() -> None:
    missing_kernel = build_resolution_bridge(
        physical_high_distribution={30.0: 0.5, 32.0: 0.5},
        resolution_contract=_contract(),
    )
    uncovered = build_resolution_bridge(
        physical_high_distribution={30.0: 1.0},
        resolution_contract=_contract(
            physical_to_settlement_kernel={"30": [{"settlement_value_c": 100.0, "probability": 1.0}]}
        ),
    )

    assert missing_kernel["ok"] is False
    assert "physical_to_settlement_kernel_missing_for_x" in missing_kernel["block_reasons"]
    assert uncovered["ok"] is False
    assert "settlement_scalar_outside_bucket_contract" in uncovered["block_reasons"]


def test_hard_zero_certificate_is_na_until_bridge_is_closed_and_validated() -> None:
    result = build_resolution_bridge(
        physical_high_distribution={30.0: 1.0},
        resolution_contract=_contract(bridge_closed=False, validated=False),
    )

    assert result["ok"] is True
    certificate = result["settlement_exclusion_certificates"]["31°C"]
    assert certificate["state"] == "na"
    assert certificate["reason"] == "resolution_bridge_not_closed_or_bucket_not_zero"


def test_resolution_contract_cannot_consume_market_or_edge_inputs() -> None:
    result = build_resolution_bridge(
        physical_high_distribution={30.0: 1.0},
        resolution_contract=_contract(market_price=0.45),
    )

    assert result["ok"] is False
    assert "resolution_contract_contains_forbidden_market_or_trading_input" in result["block_reasons"]


def test_zero_celsius_is_a_valid_scalar_not_a_missing_value() -> None:
    result = build_resolution_bridge(
        physical_high_distribution={0.0: 1.0},
        resolution_contract={
            "resolution_contract_id": "resolution_zero_v1",
            "resolution_source_id": "official_resolution_source",
            "resolution_local_date": "2026-01-01",
            "settlement_unit": "C",
            "physical_to_settlement_kernel": {
                "0": [{"settlement_value_c": 0.0, "probability": 1.0}],
            },
            "bucket_contract": {
                "0°C": {"lower_inclusive": -0.5, "upper_exclusive": 0.5},
            },
        },
    )

    assert result["ok"] is True
    assert result["market_bucket_probabilities"] == {"0°C": 1.0}


def test_open_end_market_buckets_remain_explicit_and_cover_extreme_settlements() -> None:
    result = build_resolution_bridge(
        physical_high_distribution={30.0: 0.5, 31.0: 0.5},
        resolution_contract=_contract(
            physical_to_settlement_kernel={
                "30": [{"settlement_value_c": 25.0, "probability": 1.0}],
                "31": [{"settlement_value_c": 40.0, "probability": 1.0}],
            },
            bucket_contract={
                "30°C or below": {"lower_inclusive": None, "upper_exclusive": 30.5},
                "31°C or higher": {"lower_inclusive": 30.5, "upper_exclusive": None},
            },
        ),
    )

    assert result["ok"] is True
    assert result["market_bucket_probabilities"] == {"30°C or below": 0.5, "31°C or higher": 0.5}


def _lineage(*, source_id: str, source_observation_id: str, observed_at: str) -> dict:
    return {
        "source_id": source_id,
        "source_observation_ids": [source_observation_id],
        "raw_sha256": hashlib.sha256(source_observation_id.encode("utf-8")).hexdigest(),
        "raw_object_ref": f"raw://{source_observation_id}",
        "observed_at": observed_at,
        "finality": "final",
    }


def test_final_x_s_b_label_requires_raw_lineage_kernel_support_and_exact_bucket() -> None:
    contract = _contract(
        physical_to_settlement_kernel={"30": [{"settlement_value_c": 31.0, "probability": 1.0}]},
    )
    label = build_final_resolution_label(
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        physical_high_label={
            "final_physical_high_c": 30.0,
            **_lineage(
                source_id="physical_truth_zuuu",
                source_observation_id="physical-20260703",
                observed_at="2026-07-03T18:00:00Z",
            ),
        },
        settlement_label={
            "settlement_scalar_c": 31.0,
            **_lineage(
                source_id="official_resolution_source",
                source_observation_id="settlement-20260703",
                observed_at="2026-07-03T19:00:00Z",
            ),
        },
        resolution_contract=contract,
        label_available_at="2026-07-03T19:00:00Z",
        asserted_market_bucket="31°C",
    )

    assert label["ok"] is True
    assert label["final_physical_high_c"] == 30.0
    assert label["settlement_scalar_c"] == 31.0
    assert label["market_settlement_bucket"] == "31°C"
    assert validate_final_resolution_label(label)["ok"] is True


def test_final_label_fails_closed_for_unproven_lineage_kernel_or_bucket() -> None:
    physical = {
        "final_physical_high_c": 30.0,
        **_lineage(
            source_id="physical_truth_zuuu",
            source_observation_id="physical-20260703",
            observed_at="2026-07-03T18:00:00Z",
        ),
    }
    settlement = {
        "settlement_scalar_c": 31.0,
        **_lineage(
            source_id="official_resolution_source",
            source_observation_id="settlement-20260703",
            observed_at="2026-07-03T19:00:00Z",
        ),
    }
    missing_raw = dict(physical)
    missing_raw.pop("raw_object_ref")
    without_lineage = build_final_resolution_label(
        target_identity="target",
        event_date="2026-07-03",
        physical_high_label=missing_raw,
        settlement_label=settlement,
        resolution_contract=_contract(),
        label_available_at="2026-07-03T19:00:00Z",
    )
    unsupported_s = build_final_resolution_label(
        target_identity="target",
        event_date="2026-07-03",
        physical_high_label=physical,
        settlement_label=settlement,
        resolution_contract=_contract(),
        label_available_at="2026-07-03T19:00:00Z",
    )
    wrong_bucket = build_final_resolution_label(
        target_identity="target",
        event_date="2026-07-03",
        physical_high_label=physical,
        settlement_label={**settlement, "settlement_scalar_c": 30.0},
        resolution_contract=_contract(),
        label_available_at="2026-07-03T19:00:00Z",
        asserted_market_bucket="31°C",
    )

    assert without_lineage["ok"] is False
    assert "physical_high_raw_object_ref_missing" in without_lineage["block_reasons"]
    assert unsupported_s["ok"] is False
    assert "settlement_scalar_not_supported_by_physical_to_settlement_kernel" in unsupported_s["block_reasons"]
    assert wrong_bucket["ok"] is False
    assert "asserted_market_bucket_does_not_match_resolution_contract" in wrong_bucket["block_reasons"]

from __future__ import annotations

from strong_observation_lr_artifact_contract import REQUIRED_FEATURE_IDS
from strong_physical_evidence_projection_runtime import build_strong_physical_evidence_projection


def _event(source_id: str = "zuuu_metar_raw_amsc") -> dict:
    return {
        "canonical_observation_event_id": "canobs_evidence_1234567890",
        "state": "accepted_primary",
        "selected_source_id": source_id,
        "parsed_observation": {"temperature_c": 30.0},
    }


def _update() -> dict:
    return {
        "ok": True,
        "state": "applied",
        "canonical_observation_event_id": "canobs_evidence_1234567890",
        "strong_observation_update_id": "stupd_evidence_1234567890",
        "model_artifact_sha256": "strong-artifact-evidence",
        "strong_feature_trace_by_cell": [
            {
                "temperature_c": 30.0,
                "pre_state": "warming",
                "feature_values": {feature_id: 1.0 for feature_id in REQUIRED_FEATURE_IDS},
                "feature_categories": {feature_id: "handled" for feature_id in REQUIRED_FEATURE_IDS},
            }
        ],
    }


def _build(**overrides) -> dict:
    values = {
        "previous_market_bucket_probabilities": {"29C": 0.4, "30C": 0.4, "31C": 0.2},
        "posterior_market_bucket_probabilities": {"29C": 0.1, "30C": 0.7, "31C": 0.2},
        "bucket_contract": {
            "29C": {"lower_inclusive": 28.5, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5},
        },
        "settlement_exclusion_certificates": {
            "29C": {"state": "valid", "reason": "physical_support_zero_under_closed_resolution_bridge"},
            "30C": {"state": "na"},
            "31C": {"state": "na"},
        },
        "canonical_observation_event": _event(),
        "strong_observation_update": _update(),
        "observed_daily_high_c": 30.0,
        "observed_max_lower_bound_c": 30.0,
        "realization_policy_artifact_sha256": "realization-policy-evidence",
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
    }
    values.update(overrides)
    return build_strong_physical_evidence_projection(**values)


def test_composer_projects_top_support_and_amsc_observed_floor_exclusion() -> None:
    evidence = _build(
        posterior_market_bucket_probabilities={"29C": 0.0, "30C": 0.8, "31C": 0.2},
    )

    assert evidence["ok"] is True
    assert evidence["top_bucket"] == "30C"
    assert evidence["top_bucket_joint_strong_support"] is True
    assert evidence["bucket_evidence"]["29C"]["exclusion_kind"] == "observed_daily_high_exceeded"
    assert evidence["safe_no_observed_max_c"] == 30.0
    assert evidence["boundary"]["physical_x_not_compared_directly_to_market_bucket_b"] is True
    assert evidence["bucket_evidence"]["31C"]["excluded"] is False
    assert evidence["boundary"]["does_not_create_probability"] is True


def test_remaining_warming_exclusion_requires_low_post_and_negative_joint_update() -> None:
    evidence = _build(
        previous_market_bucket_probabilities={"29C": 0.2, "30C": 0.6, "31C": 0.2},
        posterior_market_bucket_probabilities={"29C": 0.199, "30C": 0.8, "31C": 0.001},
    )

    upper = evidence["bucket_evidence"]["31C"]
    assert upper["excluded"] is True
    assert upper["exclusion_kind"] == "remaining_warming_unattainable"
    assert upper["exclusion_confidence"] == 0.999


def test_non_amsc_event_cannot_create_monotonic_safe_no_floor() -> None:
    evidence = _build(canonical_observation_event=_event("awc_metar"))

    assert evidence["ok"] is True
    assert evidence["safe_no_amsc_observation"] is False
    assert evidence["bucket_evidence"]["29C"]["excluded"] is False


def test_later_cooling_report_preserves_monotonic_amsc_daily_high_floor() -> None:
    event = _event()
    event["parsed_observation"]["temperature_c"] = 29.0
    evidence = _build(
        canonical_observation_event=event,
        observed_daily_high_c=30.0,
        observed_max_lower_bound_c=30.0,
        posterior_market_bucket_probabilities={"29C": 0.0, "30C": 0.8, "31C": 0.2},
    )

    assert evidence["ok"] is True
    assert evidence["safe_no_observed_max_c"] == 30.0
    assert evidence["bucket_evidence"]["29C"]["exclusion_kind"] == "observed_daily_high_exceeded"


def test_zero_market_bucket_without_closed_bridge_certificate_is_not_hard_excluded() -> None:
    evidence = _build(
        posterior_market_bucket_probabilities={"29C": 0.0, "30C": 0.8, "31C": 0.2},
        settlement_exclusion_certificates={
            "29C": {"state": "na"}, "30C": {"state": "na"}, "31C": {"state": "na"},
        },
    )

    assert evidence["ok"] is True
    assert evidence["bucket_evidence"]["29C"]["exclusion_kind"] != "observed_daily_high_exceeded"


def test_missing_joint_feature_trace_fails_closed() -> None:
    update = _update()
    update["strong_feature_trace_by_cell"][0]["feature_categories"].pop(REQUIRED_FEATURE_IDS[-1])
    evidence = _build(strong_observation_update=update)

    assert evidence["ok"] is False
    assert "strong_evidence_joint_feature_trace_incomplete" in evidence["block_reasons"]


def test_strong_evidence_accepts_real_market_open_edge_buckets() -> None:
    evidence = _build(
        bucket_contract={
            "29C": {"lower_inclusive": None, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C": {"lower_inclusive": 30.5, "upper_exclusive": None},
        }
    )
    assert evidence["ok"] is True

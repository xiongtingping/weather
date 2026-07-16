from forecast_source_contextual_weight_model import fit_contextual_stacking
from forecast_weak_likelihood_runtime import build_forecast_weak_likelihood


SOURCES = [
    {
        "source_event_id": "a-event",
        "source_id": "a",
        "source_class": "forecast_weak_source",
        "bucket_probabilities": {"29": 0.1, "30": 0.9},
    },
    {
        "source_event_id": "b-event",
        "source_id": "b",
        "source_class": "forecast_weak_source",
        "bucket_probabilities": {"29": 0.9, "30": 0.1},
    },
]


def _artifact(*, approved=False):
    samples = [
        {
            "event_date_local": f"2026-01-{day:02d}",
            "lead_hours": 12,
            "context": {},
            "winning_bucket": "30",
            "distributions": {"a": {"29": 0.1, "30": 0.9}, "b": {"29": 0.9, "30": 0.1}},
        }
        for day in range(1, 8)
    ]
    artifact = fit_contextual_stacking(
        samples,
        provider_families={"a": "shared", "b": "shared"},
        max_iterations=150,
        source_cap=0.9,
        family_cap=0.9,
    )
    artifact["artifact_sha256"] = "artifact-sha"
    if approved:
        artifact.update({"production_ready": True, "runtime_integrated": True, "approval_state": "approved"})
    return artifact


def test_shadow_artifact_does_not_change_runtime_likelihood():
    champion = build_forecast_weak_likelihood(buckets=["29", "30"], source_observations=SOURCES)
    shadow = build_forecast_weak_likelihood(
        buckets=["29", "30"],
        source_observations=SOURCES,
        weight_artifact=_artifact(),
    )
    assert shadow["forecast_weak_likelihood_map"] == champion["forecast_weak_likelihood_map"]
    assert shadow["contextual_weight_lane"]["state"] == "shadow_only"
    assert shadow["contextual_weight_lane"]["candidate_tv_distance"] > 0.0
    assert shadow["contextual_weight_lane"]["apply_to_runtime"] is False


def test_only_explicitly_approved_artifact_can_change_runtime_likelihood():
    result = build_forecast_weak_likelihood(
        buckets=["29", "30"],
        source_observations=SOURCES,
        weight_artifact=_artifact(approved=True),
    )
    assert result["contextual_weight_lane"]["state"] == "approved"
    assert result["contextual_weight_lane"]["apply_to_runtime"] is True
    assert result["forecast_weak_likelihood_map"]["30"] > result["forecast_weak_likelihood_map"]["29"]
    assert result["precision_cap"]["total_capped_precision"] > 0.0


def test_curve_override_is_rejected_and_cannot_change_weak_weights():
    artifact = _artifact()
    curve = {
        "state": "available",
        "curve_state_id": "curve-1",
        "effective_weights": {"a": 0.95, "b": 0.05},
        "contract": {
            "amsc_curve_reliability_consumed_once": True,
            "curve_reweighting_changes_total_weak_precision": False,
            "curve_reweighting_creates_observation_likelihood": False,
            "metar_strong_likelihood_owner_unchanged": True,
            "hard_lock_created": False,
        },
    }
    result = build_forecast_weak_likelihood(
        buckets=["29", "30"],
        source_observations=SOURCES,
        weight_artifact=artifact,
        curve_state=curve,
    )
    lane = result["contextual_weight_lane"]
    assert lane["curve_applied"] is False
    assert lane["curve_state_rejected"] is True
    assert lane["curve_state_rejected_reason"] == "d0_curve_weight_override_retired_strong_likelihood_owns_observation_interaction"
    assert lane["effective_weights"] == lane["base_weights"]
    assert lane["curve_reweighting_changes_total_weak_precision"] is False
    assert result["boundary"]["amsc_curve_reliability_consumed_once"] is False


def test_all_source_bad_curve_state_cannot_shrink_forecast_only_candidate():
    artifact = _artifact()
    base = build_forecast_weak_likelihood(
        buckets=["29", "30"], source_observations=SOURCES, weight_artifact=artifact
    )
    curve = {
        "state": "available",
        "curve_state_id": "curve-bad",
        "effective_weights": base["contextual_weight_lane"]["base_weights"],
        "weak_layer_uniform_shrinkage": 0.5,
        "contract": {
            "amsc_curve_reliability_consumed_once": True,
            "curve_reweighting_changes_total_weak_precision": False,
            "curve_reweighting_creates_observation_likelihood": False,
            "metar_strong_likelihood_owner_unchanged": True,
            "hard_lock_created": False,
        },
    }
    result = build_forecast_weak_likelihood(
        buckets=["29", "30"], source_observations=SOURCES, weight_artifact=artifact, curve_state=curve
    )
    before = base["contextual_weight_lane"]["candidate_distribution"]["30"]
    after = result["contextual_weight_lane"]["candidate_distribution"]["30"]
    assert after == before
    assert result["contextual_weight_lane"]["curve_state_rejected"] is True
    assert result["contextual_weight_lane"]["curve_reweighting_changes_total_weak_precision"] is False
